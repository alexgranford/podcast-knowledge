#!/usr/bin/env python3
"""podshows — the registry of every show the user has had processed, kept up to date without review.

    podshows.py list                               shows, monitored or not, with hosts
    podshows.py monitor <show>  / unmonitor <show> follow a show for new episodes (show: name, Apple link or id)
    podshows.py hosts <show> = A, B                the user's hosts for a show (locked: never changed automatically)
    podshows.py cohost <show> = C                  a locked recurring co-host
    podshows.py not-host <show> = C                C is never a host of this show
    podshows.py refresh [<show>]                   recompute hosts, fixes, advertisers from the notes
    podshows.py render                             rewrite the Shows.md overview in the notes folder
    podshows.py digest                             changes since the last digest (the weekly cron job's call)

shows.json (PODNOTE_STATE) holds facts about shows only, keyed by Apple show id: title, folder, country,
publisher, feed, Wikidata item, monitor flag, hosts, show-scoped name fixes and advertisers. Episodes are
not stored: they are the notes in the show's folder, PODNOTE_ROOT/PODNOTE_NOTES_DIR/<folder>/ (the
podcasts folder holds one folder per show, plus Podcasts.base and the generated Shows.md).

Hosts maintain themselves from evidence. Strong sources add a host at once: the user (locked), the
feed's podcast:person host tags, Wikidata's presenter, a full name in the show's title ("Signals with
Omar Haddad") and the feed author when it is a person. Episodes add the rest: whoever the notes list as
host, or who introduces themselves at the start, in 3 episodes. A role follows the share of recent
episodes (last 20, or 30 days if longer): about half or more is a regular host, 10-50% a recurring
co-host. A regular host missing from 8 recent episodes for 60 days, or a co-host missing for three of
their usual gaps (60 days at least), becomes former, with an end date; nobody is deleted, so older
episodes keep their hosts. Every change goes to shows-changes.log and the weekly digest.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import statistics
import sys
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402,F401  (importing it loads the project .env)

STATE = Path(os.environ.get("PODNOTE_STATE", "/var/tmp/podnote-state"))
ROOT = Path(os.environ.get("PODNOTE_ROOT", str(Path.home() / "Notes")))
SHOWS_DIR = Path(os.environ.get("PODNOTE_NOTES_DIR", "Podcasts"))   # relative to ROOT; one folder per show in it
WINDOW_EPISODES, WINDOW_DAYS = 20, 30
REGULAR_SHARE, COHOST_SHARE = 0.5, 0.1
WEAK_EVIDENCE = 3                      # episodes before an unconfirmed name becomes a host
RETIRE_EPISODES, RETIRE_DAYS = 8, 60
ADVERTISER_DAYS = 90

NOT_A_PERSON = re.compile(r"\b(podcasts?|media|news|network|ltd|limited|inc|llc|plc|studios?|productions?|radio|fm|"
                          r"group|company|show|daily|weekly|times|telegraph|guardian|bbc|npr|team|staff|editors?|"
                          r"wondery|audible|spotify|iheart\w*|global|institute|foundation|university|official)\b", re.I)


def looks_like_person(name: str, show: str = "") -> bool:
    """"Nadia Whitcombe", not "The Weekly Times", "The Open Press" or the show's own name."""
    words = name.split()
    return (2 <= len(words) <= 4 and words[0].lower() != "the" and not NOT_A_PERSON.search(name)
            and not re.search(r"[\d@/()]", name) and name.strip().lower() != show.strip().lower()
            and all(w[:1].isupper() or w.lower() in {"de", "van", "von", "der", "la", "le", "bin", "al"} for w in words))


# ---------------------------------------------------------------- storage

def registry_path() -> Path:
    return STATE / "shows.json"


def shows_root() -> Path:
    return ROOT / SHOWS_DIR


def load() -> dict:
    path = registry_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def editing():
    """The registry, locked for a read-modify-write; saved on a clean exit."""
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "shows.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry = load()
        yield registry
        atomic_write(registry_path(), json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def log_change(show: dict, text: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "shows-changes.log").open("a", encoding="utf-8") as stream:
        stream.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{show.get('folder') or show.get('title')}\t{text}\n")


def folder_name(title: str) -> str:
    """A short, stable folder name: the title before its first separator (":", " | ", or a spaced dash
    " - ", " – ", " — "), without characters a file name cannot hold, at most 60 characters on a word boundary."""
    short = re.split(r":\s|\s\|\s|\s[-–—]\s", title, maxsplit=1)[0]
    short = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|#^\[\]]', "", short)).strip(" .")
    if len(short) > 60:
        short = short[:61].rsplit(" ", 1)[0].rstrip(" ,;&-–—")
    return short or "Unknown show"


def find(registry: dict, query: str) -> str:
    """The id of the show a user means: an Apple link or id, or (a fragment of) its title or folder."""
    found = re.search(r"id(\d+)|^(\d{6,})$", query.strip())
    if found:
        key = found.group(1) or found.group(2)
        if key in registry:
            return key
    matches = [k for k, s in registry.items()
               if query.strip().lower() in (s.get("title", "") + " | " + s.get("folder", "")).lower()]
    if len(matches) != 1:
        raise LookupError(f"{'no show' if not matches else 'several shows'} match {query!r}")
    return matches[0]


# ---------------------------------------------------------------- shows and hosts

def ensure_show(registry: dict, show_id: str, title: str, country: str = "gb", metadata: dict | None = None) -> dict:
    """The registry entry for a show, created on first sight; feed facts refreshed when given."""
    show = registry.get(show_id)
    if show is None:
        show = registry[show_id] = {"title": title, "folder": folder_name(title), "country": country or "gb",
                                    "monitor": False, "added": date.today().isoformat(), "hosts": [],
                                    "fixes": {}, "advertisers": {}, "publisher": ""}
        log_change(show, f"new show ({title})")
    metadata = metadata or {}
    publisher = metadata.get("show_author") or metadata.get("show_owner") or ""
    if publisher and not show.get("publisher"):
        show["publisher"] = publisher
        log_change(show, f"publisher: {publisher}")
    for key, value in (("feed_url", metadata.get("feed_url")), ("title", title)):
        if value and show.get(key) != value:
            show[key] = value
    return show


def strong_hosts(show: dict, metadata: dict | None) -> list[tuple[str, str]]:
    """(name, source) from sources that need no confirmation."""
    metadata, title, found = metadata or {}, show.get("title", ""), []
    for person in (metadata.get("show_people") or []) + (metadata.get("episode_people") or []):
        if person.get("role", "host") == "host" and looks_like_person(person.get("name", ""), title):
            found.append((person["name"].strip(), "feed host tag"))
    named = re.search(r"(?:\bwith|\|)\s+(.+)$", title)
    for name in re.split(r"\s*(?:,|&|;|\band\b)\s*", named.group(1)) if named else []:
        if looks_like_person(name, title):
            found.append((name, "show title"))
    author = metadata.get("show_author") or metadata.get("show_owner") or ""
    for name in re.split(r"\s*(?:,|&|;|\band\b)\s*", author):
        if looks_like_person(name, title):
            found.append((name, "feed author"))
    for name in show.get("wikidata_presenters", []):
        found.append((name, "Wikidata presenter"))
    return found


def host_record(show: dict, name: str) -> dict | None:
    return next((h for h in show["hosts"] if h["name"].lower() == name.lower()), None)


def add_host(show: dict, name: str, source: str, role: str = "regular", locked: bool = False,
             since: str = "") -> dict:
    record = host_record(show, name)
    if record is None:
        record = {"name": name, "role": role, "status": "locked" if locked else "auto", "sources": [source],
                  "since": since, "until": "", "last_seen": ""}
        show["hosts"].append(record)
        log_change(show, f"{'locked ' if locked else ''}{role}: {name} ({source})")
    else:
        if source not in record["sources"]:
            record["sources"].append(source)
        if locked and (record["status"] != "locked" or record["role"] != role):
            record.update(status="locked", role=role, until="")
            log_change(show, f"locked {role}: {name} ({source})")
    return record


def hosts_on(show: dict | None, published: str) -> tuple[list[str], list[str]]:
    """(regular hosts, recurring co-hosts) of a show on an episode's date."""
    if not show:
        return [], []
    day = published or date.today().isoformat()
    current = [h for h in show.get("hosts", []) if h["role"] in ("regular", "co-host", "former")
               and (not h.get("since") or h["since"] <= day) and (not h.get("until") or h["until"] >= day)]
    regular = [h["name"] for h in current if h["role"] in ("regular", "former")]
    cohosts = [h["name"] for h in current if h["role"] == "co-host"]
    return regular, cohosts


def active_advertisers(show: dict | None, published: str = "") -> list[str]:
    if not show:
        return []
    day = date.fromisoformat(published[:10]) if published else date.today()
    return [brand for brand, seen in show.get("advertisers", {}).items()
            if abs((day - date.fromisoformat(seen[:10])).days) <= ADVERTISER_DAYS]


# ---------------------------------------------------------------- evidence from the notes

def front(text: str) -> dict:
    """The few properties the registry reads from a note."""
    import podnote
    fields = podnote.read_front(text)
    hosts = fields.get("hosts") or []
    return {"published": str(fields.get("published") or ""), "apple_episode_id": str(fields.get("apple_episode_id") or ""),
            "hosts": [str(h) for h in hosts] if isinstance(hosts, list) else []}


def episodes(show: dict) -> list[dict]:
    """The show's notes, newest first: {published, hosts (as listed, plus self-introductions), fixes}."""
    folder = shows_root() / show["folder"]
    found = []
    for path in folder.glob("*.md") if folder.is_dir() else []:
        text = path.read_text(encoding="utf-8", errors="replace")
        fields = front(text)
        if not fields["published"]:
            continue
        present = set(fields["hosts"])                     # only the note's hosts: a guest's "I'm X" is no proof
        fixes = re.findall(r"(?m)^> - (.+?) → (.+?) \(name\)$", text)
        found.append({"published": fields["published"], "hosts": present, "fixes": fixes})
    return sorted(found, key=lambda e: e["published"], reverse=True)


def refresh(registry: dict, show_id: str, metadata: dict | None = None, sponsors: list[str] | None = None,
            published: str = "") -> list[str]:
    """Recompute one show's hosts, fixes and advertisers from its notes (and the feed facts given).
    Returns the changes made, also written to the change log."""
    show = registry[show_id]
    before = json.dumps(show, sort_keys=True)
    for brand in sponsors or []:
        if published and show["advertisers"].get(brand, "") < published:
            show["advertisers"][brand] = published
    for name, source in strong_hosts(show, metadata):
        if not any(h["name"].lower() == name.lower() and h["role"] == "excluded" for h in show["hosts"]):
            add_host(show, name, source)

    notes = episodes(show)
    if notes:
        latest = date.fromisoformat(notes[0]["published"][:10])
        window = [e for e in notes if (latest - date.fromisoformat(e["published"][:10])).days <= WINDOW_DAYS]
        window = notes[:max(len(window), WINDOW_EPISODES)]
        counts: dict[str, int] = {}
        for episode in notes:
            for name in episode["hosts"]:
                counts[name] = counts.get(name, 0) + 1
        for name, total in sorted(counts.items(), key=lambda item: (-item[1], item[0])):   # weak evidence: 3 episodes
            if total >= WEAK_EVIDENCE and host_record(show, name) is None and looks_like_person(name, show["title"]):
                add_host(show, name, f"host in {total} episodes")
        for record in show["hosts"]:
            if record["status"] == "locked" or record["role"] == "excluded":
                continue
            seen = [e["published"] for e in notes if record["name"] in e["hosts"]]
            if seen:
                record["last_seen"] = max(record["last_seen"] or "", seen[0])
                record["since"] = min(filter(None, [record.get("since") or seen[-1], seen[-1]]))
            share = sum(record["name"] in e["hosts"] for e in window) / len(window)
            evidence = len(window) >= 5                    # too few episodes say nothing about roles
            if evidence and record["role"] != "former":
                role = "regular" if share >= REGULAR_SHARE else "co-host" if share >= COHOST_SHARE else record["role"]
                if role != record["role"]:
                    log_change(show, f"{record['name']}: {record['role']} -> {role} ({share:.0%} of recent episodes)")
                    record["role"] = role
            last = date.fromisoformat(record["last_seen"][:10]) if record["last_seen"] else None
            if record["role"] == "regular" and evidence:
                missed = next((i for i, e in enumerate(notes) if record["name"] in e["hosts"]), len(notes))
                stale = last is None or (latest - last).days >= RETIRE_DAYS
                if missed >= RETIRE_EPISODES and stale and record["sources"] != ["Wikidata presenter"]:
                    record.update(role="former", until=record["last_seen"] or notes[min(missed, len(notes)) - 1]["published"])
                    log_change(show, f"{record['name']}: former host (not in the last {missed} episodes)")
            elif record["role"] == "co-host" and last:
                dates = sorted(date.fromisoformat(d[:10]) for d in seen)
                gaps = [(b - a).days for a, b in zip(dates, dates[1:])] or [7]
                if (latest - last).days > max(3 * statistics.median(gaps), RETIRE_DAYS):
                    record.update(role="former", until=record["last_seen"])
                    log_change(show, f"{record['name']}: former co-host (absent {(latest - last).days} days)")
        pairs: dict[tuple[str, str], int] = {}
        for episode in notes:
            for pair in set(episode["fixes"]):
                pairs[pair] = pairs.get(pair, 0) + 1
        for (wrong, right), total in pairs.items():
            if total >= 2 and show["fixes"].get(wrong) != right:
                show["fixes"][wrong] = right
                log_change(show, f"name fix: {wrong} -> {right} (corrected in {total} episodes)")
        cutoff = (latest - timedelta(days=ADVERTISER_DAYS)).isoformat()
        for brand in [b for b, seen in show["advertisers"].items() if seen < cutoff]:
            del show["advertisers"][brand]
    return [] if json.dumps(show, sort_keys=True) == before else ["changed"]


def wikidata_presenters(registry: dict, show_id: str) -> None:
    """Presenters and publisher Wikidata lists for this Apple show id (P5842), looked up once."""
    show = registry[show_id]
    if "wikidata" in show:
        return
    import podverify                                    # its polite HTTP client and User-Agent
    query = f"""SELECT ?item (GROUP_CONCAT(DISTINCT ?p; separator="|") AS ?presenters)
      (GROUP_CONCAT(DISTINCT ?pub; separator="|") AS ?publishers) WHERE {{
      ?item wdt:P5842 "{show_id}" .
      OPTIONAL {{ ?item wdt:P371 ?x . ?x rdfs:label ?p FILTER(lang(?p)="en") }}
      OPTIONAL {{ ?item wdt:P123 ?y . ?y rdfs:label ?pub FILTER(lang(?pub)="en") }} }} GROUP BY ?item"""
    try:
        rows = podverify.http_json("https://query.wikidata.org/sparql?" + urllib.parse.urlencode(
            {"query": query, "format": "json"}), timeout=60)["results"]["bindings"]
    except Exception:
        return                                          # try again next time
    row = rows[0] if rows else {}
    show["wikidata"] = row.get("item", {}).get("value", "").rsplit("/", 1)[-1]
    show["wikidata_presenters"] = [p for p in row.get("presenters", {}).get("value", "").split("|") if p]
    publishers = [p for p in row.get("publishers", {}).get("value", "").split("|") if p]
    if publishers and not show.get("publisher"):
        show["publisher"] = publishers[0]


# ---------------------------------------------------------------- what other scripts call

def record_episode(show_id: str, title: str, country: str, metadata: dict, sponsors: list[str]) -> dict:
    """After a note is written: register the show if new, learn from it, return the show's entry."""
    with editing() as registry:
        ensure_show(registry, show_id, title, country, metadata)
        wikidata_presenters(registry, show_id)
        refresh(registry, show_id, metadata, sponsors, metadata.get("published") or "")
        return registry[show_id]


def show_for(show_id: str) -> dict | None:
    return load().get(show_id)


def monitored_since(show_id: str) -> str:
    """When the user asked to follow the show (UTC, "…Z"), or "" if not recorded."""
    return str(load().get(show_id, {}).get("monitor_since") or "")


def monitored() -> list[tuple[str, str]]:
    return [(key, show.get("country") or "gb") for key, show in sorted(load().items()) if show.get("monitor")]


# ---------------------------------------------------------------- overview and digest

def render() -> Path:
    """Shows.md in the podcasts folder, beside the show folders: a generated, read-only overview."""
    registry, rows = load(), []
    for key, show in sorted(registry.items(), key=lambda item: item[1].get("folder", "").lower()):
        notes = episodes(show)
        regular = ", ".join(h["name"] for h in show["hosts"] if h["role"] == "regular") or "—"
        cohosts = ", ".join(h["name"] for h in show["hosts"] if h["role"] == "co-host") or "—"
        link = f"[{show['folder']}](https://podcasts.apple.com/{show.get('country') or 'gb'}/podcast/id{key})"
        rows.append(f"| {link} | {'✅' if show.get('monitor') else ''} | {show.get('publisher') or '—'} | {regular} | "
                    f"{cohosts} | {len(notes)} | {notes[0]['published'] if notes else '—'} |")
    path = shows_root() / "Shows.md"
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    import podnote
    fields = podnote.read_front(old)
    created = podnote.utc(str(fields.get("created") or "")) or now      # unknown start: now
    kept = [str(t) for t in fields.get("tags") or [] if isinstance(fields.get("tags"), list)]
    tags = list(dict.fromkeys([t for t in kept if t != podnote.SCRIPT_TAG] + [podnote.SCRIPT_TAG] * bool(podnote.SCRIPT_TAG)))
    front = ["---", "origin: agent", f'created: "{created}"', f'updated: "{now}"',   # a script's note: no model,
             f"tags: [{', '.join(tags)}]" if tags else "", "---"]               # the folder's script marker
    front = [line for line in front if line]
    text = "\n".join([
        *front, "# Podcast shows", "",
        "> [!info] Generated from the show registry — edits here are overwritten.",
        "> Change it through Hermes: \"monitor <show>\", \"stop monitoring <show>\", \"hosts of <show> are A, B\",",
        "> \"C co-hosts <show>\", \"C is not a host of <show>\". Monitored shows are checked every 30 minutes.", "",
        "| Show | Monitored | Publisher | Hosts | Co-hosts | Episodes | Latest |",
        "| --- | --- | --- | --- | --- | --- | --- |", *rows, ""])
    same = lambda t: re.sub(r'(?m)^updated: .*$', "", t)
    if old and same(old) == same(text):                   # nothing new: the file, and its `updated`, stay
        return path
    issues = podnote.validate(text, episode=False)        # an invalid overview is not written
    if issues:
        raise RuntimeError("Shows.md fails the metadata check: " + ", ".join(issues))
    atomic_write(path, text)
    return path


def digest() -> str:
    """Registry changes since the last digest, one line each; "" when nothing changed."""
    log, mark = STATE / "shows-changes.log", STATE / "shows-digest.json"
    since = json.loads(mark.read_text(encoding="utf-8")).get("until", "") if mark.is_file() else ""
    lines = [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines()] if log.is_file() else []
    new = [parts for parts in lines if len(parts) == 3 and parts[0] > since]
    if new:
        atomic_write(mark, json.dumps({"until": new[-1][0]}) + "\n")
    if not new:
        return ""
    by_show: dict[str, list[str]] = {}
    for _, show, text in new:
        by_show.setdefault(show, []).append(text)
    return "Podcast shows — changes this week:\n" + "\n".join(
        f"• {show}: " + "; ".join(texts[-6:]) for show, texts in sorted(by_show.items()))


def register_link(registry: dict, link: str) -> str:
    """Enter a show from its Apple Podcasts link: title and storefront from Apple's lookup."""
    import podfetch
    show_id, _, country = podfetch.parse_apple_url(link.strip())
    status, body, _ = podfetch.http_get(f"{podfetch.ITUNES_LOOKUP}?" + urllib.parse.urlencode({"id": show_id}))
    results = json.loads(body).get("results", []) if status == 200 else []
    if not results:
        raise LookupError(f"Apple does not know show {show_id}")
    ensure_show(registry, show_id, results[0].get("collectionName") or show_id, country or "gb",
                {"show_author": results[0].get("artistName", ""), "feed_url": results[0].get("feedUrl", "")})
    return show_id


# ---------------------------------------------------------------- command line (the agent's)

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["list", "monitor", "unmonitor", "hosts", "cohost", "not-host",
                                            "refresh", "render", "digest"])
    parser.add_argument("rest", nargs="*")
    args = parser.parse_args()
    text = " ".join(args.rest)
    if args.command == "digest":
        message = digest()
        if message:
            print(message)
        return 0
    if args.command == "render":
        print(render())
        return 0
    if args.command == "list":
        for key, show in sorted(load().items(), key=lambda item: item[1].get("folder", "")):
            hosts = ", ".join(f"{h['name']} ({h['role']})" for h in show["hosts"] if h["role"] != "excluded")
            print(f"{show['folder']}{' [monitored]' if show.get('monitor') else ''} — {hosts or 'hosts unknown'}")
        return 0
    with editing() as registry:
        if args.command == "refresh":
            for key in ([find(registry, text)] if text else list(registry)):
                refresh(registry, key)
            message = "Refreshed."
        else:
            target, _, names = text.partition("=")
            try:
                key = find(registry, target)
            except LookupError:
                if args.command != "monitor" or not re.search(r"id\d+", target):
                    raise
                key = register_link(registry, target)            # a show link never processed before
            show = registry[key]
            people = [n.strip() for n in re.split(r",|\band\b", names) if n.strip()]
            if args.command in ("monitor", "unmonitor"):
                wanted = args.command == "monitor"
                if show.get("monitor") != wanted:              # a repeated command changes nothing, logs nothing
                    show["monitor"] = wanted
                    if wanted:
                        show["monitor_since"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    log_change(show, "monitored" if wanted else "no longer monitored")
                message = f"{show['folder']}: {'monitored' if show['monitor'] else 'not monitored'}."
            else:
                role = {"hosts": "regular", "cohost": "co-host", "not-host": "excluded"}[args.command]
                if not people:
                    raise SystemExit("give the names after '=', e.g. hosts Quiet Hours = Lena Wells, Tom Hart")
                for name in people:
                    add_host(show, name, "user", role=role, locked=True)
                message = f"{show['folder']}: {', '.join(people)} recorded as {role}."
    render()
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
