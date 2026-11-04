#!/usr/bin/env python3
"""podshows — the registry of every show the user has had processed, kept up to date without review.

    podshows.py list                               shows, followed or not, with hosts
    podshows.py follow <show>  / unfollow <show>   follow a show for new episodes (show: name, Apple link or id,
                                                   or a YouTube channel link for a show only on YouTube); the
                                                   person asking becomes one of its followers (monitor and
                                                   unmonitor are the same commands)
    podshows.py mine                               the shows the person asking follows
    podshows.py follow <show> = <person>, …        (admin) subscribe others; unfollow <show> = <person> likewise
    podshows.py mine <person>  / followers <show>  (admin) what someone follows; who follows a show
    podshows.py link <show> = <YouTube link>       watch the show's YouTube channel or playlist too
    podshows.py unlink <show>                      stop watching it on YouTube
    podshows.py priority <show> = youtube, apple   which source of a show comes first (default: Apple, then YouTube)
    podshows.py link-wikidata                      link every followed show whose Wikidata item names its channel
    podshows.py language <show> = <language>       the language the show is spoken in (unset: English, but an
                                                   episode that looks otherwise is held for the user's answer)
    podshows.py hosts <show> = A, B                the user's hosts for a show (locked: never changed automatically)
    podshows.py cohost <show> = C                  a locked recurring co-host
    podshows.py not-host <show> = C                C is never a host of this show
    podshows.py refresh [<show>]                   recompute hosts, fixes, advertisers from the notes
    podshows.py render                             rewrite the Shows.md overview in the notes folder
    podshows.py digest                             changes since the last digest (the weekly cron job's call)

shows.json (PODNOTE_STATE) holds facts about shows only, keyed by Apple show id (yt:<channel id> for a show
only on YouTube): title, folder, country, publisher, feed, Wikidata item, followers, hosts, show-scoped
name fixes, advertisers, its language once the user has confirmed it (`language`, an ISO 639-1 code), and its
`sources`: Apple and, when linked, a YouTube channel (and playlist), each with a priority.
A followed show is watched on every source; an episode is made from the highest-priority source that
has it, and a copy from another source is recognised as the same episode. Episodes are
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

Several people can use one installation (users.py). A show's `followers` are the people who follow it:
each gets a message for every new note of the show. A show is monitored while it has a follower.
Anyone may follow, unfollow and list for themselves; the admin may also
do it for others (who are told), and changing a show's hosts, links or language is the admin's alone.
The admin is told when a member follows or unfollows a show.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402,F401  (importing it loads the project .env)
from files import atomic_write  # noqa: E402
import notes  # noqa: E402  (reading notes)

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
    """The id of the show a user means: an Apple link or id, a linked YouTube channel or playlist link,
    or (a fragment of) its title or folder."""
    if query.strip() in registry:
        return query.strip()
    if re.search(r"youtube\.com|youtu\.be", query):
        import sources
        ref = sources.parse(query.strip())
        for key, show in sorted(registry.items(), key=lambda item: item[0].startswith("yt:")):
            linked = youtube_link(show, key)
            if ref.show and ref.show.lower() in {str(linked.get(k) or "").lower() for k in ("channel", "playlist", "handle")} - {""}:
                return key
        raise LookupError(f"no show is linked to {query.strip()}")
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
                                    "added": date.today().isoformat(), "hosts": [],
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
    if metadata.get("language_source") == "user" and metadata.get("language") and not show.get("language"):
        set_language(show, str(metadata["language"]))     # a new show takes the language its first link was given
    return show


def set_language(show: dict, code: str) -> None:
    if show.get("language") != code:
        import languages
        show["language"] = code
        log_change(show, f"language: {languages.name(code)}")


def language_of(show_id: str) -> str:
    """The show's confirmed language code; "" when the user has not confirmed one."""
    return str((show_for(show_id) or {}).get("language") or "")


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
    fields = notes.read_front(text)
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

    made = episodes(show)
    if made:
        latest = date.fromisoformat(made[0]["published"][:10])
        window = [e for e in made if (latest - date.fromisoformat(e["published"][:10])).days <= WINDOW_DAYS]
        window = made[:max(len(window), WINDOW_EPISODES)]
        counts: dict[str, int] = {}
        for episode in made:
            for name in episode["hosts"]:
                counts[name] = counts.get(name, 0) + 1
        for name, total in sorted(counts.items(), key=lambda item: (-item[1], item[0])):   # weak evidence: 3 episodes
            if total >= WEAK_EVIDENCE and host_record(show, name) is None and looks_like_person(name, show["title"]):
                add_host(show, name, f"host in {total} episodes")
        for record in show["hosts"]:
            if record["status"] == "locked" or record["role"] == "excluded":
                continue
            seen = [e["published"] for e in made if record["name"] in e["hosts"]]
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
                missed = next((i for i, e in enumerate(made) if record["name"] in e["hosts"]), len(made))
                stale = last is None or (latest - last).days >= RETIRE_DAYS
                if missed >= RETIRE_EPISODES and stale and record["sources"] != ["Wikidata presenter"]:
                    record.update(role="former", until=record["last_seen"] or made[min(missed, len(made)) - 1]["published"])
                    log_change(show, f"{record['name']}: former host (not in the last {missed} episodes)")
            elif record["role"] == "co-host" and last:
                dates = sorted(date.fromisoformat(d[:10]) for d in seen)
                gaps = [(b - a).days for a, b in zip(dates, dates[1:])] or [7]
                if (latest - last).days > max(3 * statistics.median(gaps), RETIRE_DAYS):
                    record.update(role="former", until=record["last_seen"])
                    log_change(show, f"{record['name']}: former co-host (absent {(latest - last).days} days)")
        pairs: dict[tuple[str, str], int] = {}
        for episode in made:
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
    import web                                          # the polite HTTP client and User-Agent
    query = f"""SELECT ?item (GROUP_CONCAT(DISTINCT ?p; separator="|") AS ?presenters)
      (GROUP_CONCAT(DISTINCT ?pub; separator="|") AS ?publishers) WHERE {{
      ?item wdt:P5842 "{show_id}" .
      OPTIONAL {{ ?item wdt:P371 ?x . ?x rdfs:label ?p FILTER(lang(?p)="en") }}
      OPTIONAL {{ ?item wdt:P123 ?y . ?y rdfs:label ?pub FILTER(lang(?pub)="en") }} }} GROUP BY ?item"""
    try:
        rows = web.http_json("https://query.wikidata.org/sparql?" + urllib.parse.urlencode(
            {"query": query, "format": "json"}), timeout=60)["results"]["bindings"]
    except Exception:
        return                                          # try again next time
    row = rows[0] if rows else {}
    show["wikidata"] = row.get("item", {}).get("value", "").rsplit("/", 1)[-1]
    show["wikidata_presenters"] = [p for p in row.get("presenters", {}).get("value", "").split("|") if p]
    publishers = [p for p in row.get("publishers", {}).get("value", "").split("|") if p]
    if publishers and not show.get("publisher"):
        show["publisher"] = publishers[0]


def wikidata_channel(registry: dict, show_id: str) -> str:
    """The YouTube channel id (P2397) on the Wikidata item of this Apple show (P5842), looked up once;
    "" when there is none."""
    show = registry[show_id]
    if "youtube_checked" in show or show_id.startswith("yt:"):
        return ""
    import web
    query = f"""SELECT ?channel WHERE {{ ?item wdt:P5842 "{show_id}" ; wdt:P2397 ?channel . }}"""
    try:
        rows = web.http_json("https://query.wikidata.org/sparql?" + urllib.parse.urlencode(
            {"query": query, "format": "json"}), timeout=60)["results"]["bindings"]
    except Exception:
        return ""                                       # try again next time
    show["youtube_checked"] = date.today().isoformat()
    channels = sorted({row["channel"]["value"] for row in rows if re.fullmatch(r"UC[\w-]{22}", row["channel"]["value"])})
    return channels[0] if len(channels) == 1 else ""


def link_youtube(registry: dict, show_id: str, link: str, source: str) -> str:
    """Link a show to a YouTube channel or playlist (its podcast playlist found when the link names a
    channel); a show only on YouTube becomes yt:<channel>. Returns the line to report."""
    import sources
    found = sources.youtube.channel(sources.parse(link))
    show = registry[show_id]
    before = youtube_link(show, show_id)
    set_youtube(show, show_id, {"channel": found["channel"], "playlist": found["playlist"], "title": found["title"],
                                "handle": found.get("handle", ""), "linked_by": source,
                                "since": before.get("since") if before.get("channel") == found["channel"]
                                else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    if before.get("channel") != found["channel"] or before.get("playlist") != found["playlist"]:
        log_change(show, f"YouTube: {found['title']} ({'podcast playlist' if found['playlist'] else 'channel videos'}, {source})")
    return f"{show['folder']}: linked to YouTube {found['title']}" + (" (podcast playlist)." if found["playlist"] else " (its videos).")


def link_from_wikidata(registry: dict, show_id: str) -> str:
    """Link the show to the YouTube channel its Wikidata item names, once; "" when it names none."""
    if youtube_link(registry[show_id], show_id).get("linked_by"):    # linked already
        return ""
    channel = wikidata_channel(registry, show_id)
    if not channel:
        return ""
    try:
        return link_youtube(registry, show_id, f"https://www.youtube.com/channel/{channel}", "Wikidata")
    except Exception as error:                          # yt-dlp missing or YouTube down: the next try may work
        registry[show_id].pop("youtube_checked", None)
        return f"{registry[show_id]['folder']}: YouTube channel {channel} not linked ({error})"


def register_youtube(registry: dict, link: str) -> str:
    """Enter a show that is only on YouTube from its channel or playlist link: yt:<channel id>."""
    import sources
    found = sources.youtube.channel(sources.parse(link))
    known = [k for k, show in sorted(registry.items(), key=lambda item: item[0].startswith("yt:"))
             if youtube_link(show, k).get("channel") == found["channel"] or k == f"yt:{found['channel']}"]
    if known:                                           # already here (under another link, or an Apple show)
        return known[0]
    key = f"yt:{found['channel']}"
    ensure_show(registry, key, found["title"] or key, "gb", {"show_author": found["title"]})
    link_youtube(registry, key, link, "user")
    return key


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


def sources_of(key: str, show: dict) -> list[dict]:
    """The show's sources, highest priority first (1 is the highest): an Apple Podcasts feed
    {"type": "apple", "id", "country", "priority"} and a YouTube channel {"type": "youtube", "channel",
    "playlist", "handle", "title", "linked_by", "since", "priority"}. Stored as show["sources"]; a show not
    given any yet has the one its key names (an Apple show id, or yt:<channel>)."""
    if "sources" in show:
        found = [dict(source) for source in show["sources"]]
    elif key.startswith("yt:"):
        found = [{"type": "youtube", "channel": key[3:], "playlist": "", "priority": 1}]
    else:
        found = [{"type": "apple", "id": key, "country": show.get("country") or "gb", "priority": 1}] if key.isdigit() else []
    return sorted(found, key=lambda source: source.get("priority", 99))


def youtube_link(show: dict, key: str = "") -> dict:
    """The show's YouTube source ({} when it has none)."""
    return next((s for s in sources_of(key, show) if s["type"] == "youtube" and s.get("channel")), {})


def set_youtube(show: dict, key: str, link: dict | None) -> None:
    """Add, replace or (link None) remove the show's YouTube source, keeping the other sources and
    their priorities; a new YouTube source comes after the existing ones."""
    others = [s for s in sources_of(key, show) if s["type"] != "youtube"]
    if link:
        before = next((s for s in sources_of(key, show) if s["type"] == "youtube"), {})
        link = {"type": "youtube", **link, "priority": before.get("priority") or len(others) + 1}
    show["sources"] = sorted(others + ([link] if link else []), key=lambda source: source.get("priority", 99))


def set_priority(show: dict, key: str, order: list[str]) -> list[str]:
    """Give the show's sources the priorities of this order (types, highest first); the rest follow."""
    current = sources_of(key, show)
    types = [s["type"] for s in current]
    unknown = [t for t in order if t not in types]
    if unknown:
        raise SystemExit(f"{show.get('folder', key)} has no {', '.join(unknown)} source; it has {', '.join(types)}")
    ranked = sorted(current, key=lambda s: (order.index(s["type"]) if s["type"] in order else len(order), s.get("priority", 99)))
    for rank, source in enumerate(ranked, 1):
        source["priority"] = rank
    show["sources"] = ranked
    return [s["type"] for s in ranked]


def outranked(key: str, show: dict, source_type: str) -> bool:
    """Whether another of the show's sources comes before this one (so this one's copy waits for it)."""
    ranked = [s["type"] for s in sources_of(key, show)]
    return source_type in ranked and ranked.index(source_type) > 0


def key_for_channel(channel: str) -> str:
    """The registry key of the show a YouTube channel is linked to: an Apple show it belongs to, else
    the channel's own yt:<channel> entry; "" when the registry does not know the channel."""
    linked = [key for key, show in sorted(load().items()) if youtube_link(show, key).get("channel") == channel]
    return next((key for key in linked if not key.startswith("yt:")), linked[0] if linked else "")


def show_key(metadata: dict) -> tuple[str, str]:
    """(registry key, storefront) of an episode's show: its Apple show id, from its metadata or its Apple
    link; for a YouTube video, the show its connector chose (youtube.show_key: the show linked to its
    channel and playlist), else yt:<channel id>."""
    if metadata.get("youtube_channel_id") and not metadata.get("apple_show_id"):
        channel = str(metadata["youtube_channel_id"])
        return (metadata.get("youtube_show") or key_for_channel(channel) or f"yt:{channel}",
                metadata.get("country") or "gb")
    found = re.search(r"podcasts\.apple\.com/(\w\w)/podcast/[^?]*?id(\d+)", metadata.get("source_url", ""))
    return (metadata.get("apple_show_id") or (found.group(2) if found else ""),
            metadata.get("country") or (found.group(1) if found else "gb"))


def monitored_since(show_id: str) -> str:
    """When the user asked to follow the show (UTC, "…Z"), or "" if not recorded."""
    return str(load().get(show_id, {}).get("monitor_since") or "")


def followers_of(show: dict) -> list[str]:
    """The keys of the people who follow a show. A show is monitored while it has one."""
    return list(show.get("followers") or [])


def follow(show: dict, person: str, wanted: bool = True) -> bool:
    """Add (or remove) a follower. True when anything changed."""
    followers = followers_of(show)
    if (person in followers) == wanted:
        return False
    show["followers"] = followers + [person] if wanted else [k for k in followers if k != person]
    if bool(followers) != bool(show["followers"]):    # monitoring starts or stops
        if show["followers"]:
            show["monitor_since"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        log_change(show, "monitored" if show["followers"] else "no longer monitored")
    log_change(show, f"{'followed' if wanted else 'unfollowed'} by {person}")
    return True


def watch_own_channel(registry: dict, key: str) -> str:
    """A show only on YouTube that came in from one of its videos has no channel to watch yet: link its
    own channel (and its podcast playlist, if it has one), so following it really watches it."""
    if not key.startswith("yt:") or youtube_link(registry[key], key).get("linked_by"):
        return ""
    return link_youtube(registry, key, f"https://www.youtube.com/channel/{key[3:]}", "user")


def monitored() -> list[tuple[str, str]]:
    return [(key, show.get("country") or "gb") for key, show in sorted(load().items()) if followers_of(show)]


# ---------------------------------------------------------------- overview and digest

def render() -> Path:
    """Shows.md in the podcasts folder, beside the show folders: a generated, read-only overview."""
    registry, rows = load(), []
    for key, show in sorted(registry.items(), key=lambda item: item[1].get("folder", "").lower()):
        made = episodes(show)
        regular = ", ".join(h["name"] for h in show["hosts"] if h["role"] == "regular") or "—"
        cohosts = ", ".join(h["name"] for h in show["hosts"] if h["role"] == "co-host") or "—"
        channel = youtube_link(show, key).get("channel")
        link = (f"[{show['folder']}](https://www.youtube.com/channel/{key[3:]})" if key.startswith("yt:") else
                f"[{show['folder']}](https://podcasts.apple.com/{show.get('country') or 'gb'}/podcast/id{key})"
                + (f" · [YouTube](https://www.youtube.com/channel/{channel})" if channel else ""))
        if show.get("language") and show["language"] != "en":
            import languages
            link += f" ({languages.name(show['language'])})"
        followers = len(followers_of(show))
        rows.append(f"| {link} | {followers or ''} | "
                    f"{show.get('publisher') or '—'} | {regular} | "
                    f"{cohosts} | {len(made)} | {made[0]['published'] if made else '—'} |")
    path = shows_root() / "Shows.md"
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    import podnote
    fields = notes.read_front(old)
    created = notes.utc(str(fields.get("created") or "")) or now      # unknown start: now
    kept = [str(t) for t in fields.get("tags") or [] if isinstance(fields.get("tags"), list)]
    tags = list(dict.fromkeys([t for t in kept if t != podnote.SCRIPT_TAG] + [podnote.SCRIPT_TAG] * bool(podnote.SCRIPT_TAG)))
    front = ["---", "origin: agent", f'created: "{created}"', f'updated: "{now}"',   # a script's note: no model,
             f"tags: [{', '.join(tags)}]" if tags else "", "---"]               # the folder's script marker
    front = [line for line in front if line]
    text = "\n".join([
        *front, "# Podcast shows", "",
        "> [!info] Generated from the show registry — edits here are overwritten.",
        "> Change it through Hermes: \"follow <show>\", \"unfollow <show>\", \"hosts of <show> are A, B\",",
        "> \"C co-hosts <show>\", \"C is not a host of <show>\", \"watch <show> on YouTube <link>\". Followed shows are",
        "> checked every 30 minutes on each source; an episode is made from the first source (by priority) that has it.",
        "> Followers: how many people follow the show; a followed show is checked for new episodes.", "",
        "| Show | Followers | Publisher | Hosts | Co-hosts | Episodes | Latest |",
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

def assignment(text: str) -> tuple[str, str]:
    """(what, value) of "<what> = <value>". The spaced " = " separates them, so a link's own "=" (?list=,
    ?si=) never does; without one, a plain "=" does unless the text is a link (then it is all "what")."""
    found = re.split(r"\s+=\s+", text.strip(), maxsplit=1)
    if len(found) == 2:
        return found[0].strip(), found[1].strip()
    if "://" in text:
        return text.strip(), ""
    what, _, value = text.partition("=")
    return what.strip(), value.strip()


ADMIN_ONLY = {"hosts", "cohost", "not-host", "refresh", "link", "unlink", "link-wikidata", "language", "priority",
              "digest"}                         # the digest is the admin's weekly summary: reading it marks it read


class Asker:
    """Who is asking (users.current), and the messages to send once the registry is saved."""

    def __init__(self):
        import users
        self.key, self.user, self.new = users.current()
        self.notices: list[tuple[str, str]] = []        # (person, message); person "" is the admin

    @property
    def admin(self) -> bool:
        import users
        return users.is_admin(self.user)

    def send(self) -> None:
        import notify
        for who, message in self.notices:
            notify.to_user(who, message) if who else notify.to_admin(message)


def names_in_list(text: str) -> list[str]:
    """ "A, B and C" -> ["A", "B", "C"]."""
    return [name.strip() for name in re.split(r",|\band\b", text) if name.strip()]


# ---- commands that only read

def show_mine(text: str, asker: Asker) -> str:
    import users
    everyone = users.load()
    whose = users.pick(everyone, text) if text.strip() else asker.key
    mine = sorted(show["folder"] for show in load().values() if whose in followers_of(show))
    name, verb = ("You", "follow") if whose == asker.key else (everyone[whose].get("name", whose), "follows")
    return f"{name} {verb}: " + ", ".join(mine) + "." if mine else f"{name} {verb} no shows yet."


def show_followers(text: str, asker: Asker) -> str:
    import users
    registry, everyone = load(), users.load()
    show = registry[find(registry, text)]
    names = [everyone.get(key, {}).get("name", key) for key in followers_of(show)]
    return f"{show['folder']}: " + (", ".join(names) if names else "nobody follows it") + "."


def show_list(text: str, asker: Asker) -> str:
    lines = []
    for key, show in sorted(load().items(), key=lambda item: item[1].get("folder", "")):
        hosts = ", ".join(f"{h['name']} ({h['role']})" for h in show["hosts"] if h["role"] != "excluded")
        ranked = [s["type"] for s in sources_of(key, show)]
        order = f" [{' > '.join(ranked)}]" if len(ranked) > 1 else ""
        spoken = f" [{show['language']}]" if show.get("language") else ""
        followed = f" [followed by {len(followers_of(show))}]" if followers_of(show) else ""
        lines.append(f"{show['folder']}{followed}{order}{spoken} — {hosts or 'hosts unknown'}")
    return "\n".join(lines)


READ_ONLY = {"mine": show_mine, "followers": show_followers, "list": show_list,
             "digest": lambda text, asker: digest(), "render": lambda text, asker: str(render())}


# ---- commands that change one show

def follow_command(registry: dict, key: str, names: str, asker: Asker, wanted: bool) -> str:
    """Follow or unfollow: for the person asking, or (the admin) for the people named after "="."""
    import users
    show, everyone = registry[key], users.load()
    others = bool(names.strip())
    chosen = list(dict.fromkeys(users.pick(everyone, name) for name in names_in_list(names))) if others else [asker.key]
    watched = watch_own_channel(registry, key) if wanted else ""
    done = [who for who in chosen if follow(show, who, wanted)]   # every name was known before anything changed
    for who in done:
        if who != asker.key:
            asker.notices.append((who, f"{asker.user.get('name', 'The admin')} " +
                                  (f"subscribed you to {show['folder']}: each new episode's note will come to you here."
                                   if wanted else f"unsubscribed you from {show['folder']}.")))
        elif not asker.admin:
            asker.notices.append(("", f"👤 {asker.user['name']} {'follows' if wanted else 'stopped following'} {show['folder']}"))
    if others:
        named = ", ".join(everyone[who].get("name", who) for who in done)
        message = (f"{show['folder']}: nothing to change." if not done else
                   f"{show['folder']}: {named} {'now follow' if len(done) > 1 else 'now follows'} it." if wanted else
                   f"{show['folder']}: {named} no longer follow{'s' * (len(done) == 1)} it.")
    else:
        rest = [who for who in followers_of(show) if who != asker.key]
        message = (f"{show['folder']}: you follow it; each new episode's note comes to you here." if wanted else
                   f"{show['folder']}: you no longer follow it" +
                   ("." if not rest else f"; it stays monitored for {len(rest)} other follower(s)."))
    linked = watched or (link_from_wikidata(registry, key) if wanted and (done or not others) else "")
    return message + ("\n" + linked if linked else "")


def language_command(registry: dict, key: str, names: str, asker: Asker) -> str:
    import languages
    code = languages.code(names)
    if not code:
        raise SystemExit(f"not a language I know: {names.strip() or '(none)'}; e.g. language <show> = English")
    set_language(registry[key], code)
    return f"{registry[key]['folder']}: spoken in {languages.name(code)}; its episodes are transcribed and written in it."


def priority_command(registry: dict, key: str, names: str, asker: Asker) -> str:
    wanted = [{"yt": "youtube"}.get(w, w) for w in re.findall(r"[a-z]+", names.lower()) if w in ("apple", "youtube", "yt")]
    if not wanted:
        raise SystemExit("give the order after '=', e.g. priority <show> = youtube, apple")
    ranked = set_priority(registry[key], key, wanted)
    log_change(registry[key], "source priority: " + " > ".join(ranked))
    return (f"{registry[key]['folder']}: {' first, then '.join(ranked)}. A new episode from a later source waits "
            f"for the earlier one, and is made from it only if that one has none.")


def link_command(registry: dict, key: str, names: str, asker: Asker) -> str:
    if not re.search(r"youtube\.com|youtu\.be", names):
        raise SystemExit("give the YouTube link after '=', e.g. link <show> = https://www.youtube.com/@<handle>")
    return link_youtube(registry, key, names.strip(), "user")


def unlink_command(registry: dict, key: str, names: str, asker: Asker) -> str:
    show = registry[key]
    if youtube_link(show, key) and not key.startswith("yt:"):
        set_youtube(show, key, None)
        log_change(show, "YouTube: unlinked")
    show["youtube_checked"] = show.get("youtube_checked") or date.today().isoformat()   # Wikidata won't relink
    return f"{show['folder']}: not watched on YouTube."


def hosts_command(role: str):
    def run(registry: dict, key: str, names: str, asker: Asker) -> str:
        people = names_in_list(names)
        if not people:
            raise SystemExit("give the names after '=', e.g. hosts Quiet Hours = Lena Wells, Tom Hart")
        for name in people:
            add_host(registry[key], name, "user", role=role, locked=True)
        return f"{registry[key]['folder']}: {', '.join(people)} recorded as {role}."
    return run


PER_SHOW = {"follow": lambda r, k, n, a: follow_command(r, k, n, a, True),
            "unfollow": lambda r, k, n, a: follow_command(r, k, n, a, False),
            "language": language_command, "priority": priority_command, "link": link_command, "unlink": unlink_command,
            "hosts": hosts_command("regular"), "cohost": hosts_command("co-host"), "not-host": hosts_command("excluded")}


def registry_command(command: str, text: str, asker: Asker) -> str:
    """A command that changes the registry; the registry is saved when it returns."""
    with editing() as registry:
        if command == "refresh":
            for key in ([find(registry, text)] if text else list(registry)):
                refresh(registry, key)
            return "Refreshed."
        if command == "link-wikidata":
            lines = [line for key in sorted(registry) if followers_of(registry[key])
                     for line in [link_from_wikidata(registry, key)] if line]
            return "\n".join(lines) or "No monitored show has a YouTube channel on Wikidata that is not linked yet."
        target, names = assignment(text)
        try:
            key = find(registry, target)
        except LookupError:
            if command != "follow" or not re.search(r"id\d+|youtube\.com|youtu\.be", target):
                raise
            key = (register_youtube(registry, target.strip()) if re.search(r"youtube\.com|youtu\.be", target)
                   else register_link(registry, target))      # a show link never processed before
            registry[key]["added_by"] = asker.key
        return PER_SHOW[command](registry, key, names, asker)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["list", "monitor", "unmonitor", "follow", "unfollow", "mine", "followers", "hosts",
                                            "cohost", "not-host", "refresh", "render", "digest", "link", "unlink",
                                            "link-wikidata", "language", "priority"])
    parser.add_argument("rest", nargs="*")
    args = parser.parse_args()
    text = " ".join(args.rest)
    command = {"monitor": "follow", "unmonitor": "unfollow"}.get(args.command, args.command)
    import notify
    import users
    asker = Asker()
    if asker.new:
        notify.to_admin(f"👤 New user: {asker.user['name']}")
    if asker.user.get("role") == "blocked":
        print("Not available.")
        return 1
    admin = users.admin()[1].get("name", "the admin")
    if command in ADMIN_ONLY and not asker.admin:
        print(f"Only {admin} can change that; you can follow, unfollow and list shows.")
        return 1
    for_others = (command in ("follow", "unfollow") and bool(assignment(text)[1])) or (command == "mine" and text.strip()) \
        or command == "followers"
    if for_others and not asker.admin:
        print(f"Only {admin} can do that for someone else.")
        return 1
    if command in READ_ONLY:
        message = READ_ONLY[command](text, asker)
        if message:
            print(message)
        return 0
    message = registry_command(command, text, asker)
    asker.send()                                        # only now: the change is saved (before the table is
    render()                                            # redrawn, so a failed redraw loses no notice)
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
