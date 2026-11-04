#!/usr/bin/env python3
"""podqueue — a plain queue of episode links (Apple Podcasts, YouTube) in front of podnote.py.

    podqueue.py add <episode-url> [--source NAME] [--language L]   queue a link; prints one line
    podqueue.py run                                       process the next due link (the default)
    podqueue.py list                                      show the queue and any pending retries

podnote.py stays a single run; this file only decides which link it runs next. A Hermes cron job
(script mode, no model) calls `run` every few minutes: one episode per call, nothing printed when
there is nothing to report, one line per finished or failed episode otherwise (delivered to
Telegram by the cron job).

Retries only for an episode that was not processed - Apple has not indexed it yet, YouTube is
temporarily refusing or a premiere has not started, the model server is unreachable, the model
server is busy - at 1, 3, 6 and 12 hours after its first failure, then it is
marked failed. A verification pass that fails is retried on its own (1, 3, 6, 12 hours after its latest
try), silently: on a tick with nothing due, or on any tick once it is VERIFY_OVERDUE late.
A run that dies mid-way (a timeout or a crash) has the models it left on the server unloaded. A long
episode (over about three hours) cannot be done in one 40-minute run: a run stopped by the time limit
that saved new work (audio, transcribed windows, note sections) is carried on at the next tick, up to
RESUMES times, without counting as a failure. Another podnote run holding the lock is not a failure: the link simply waits. A
partial note is a failure (the old note stays). Anything else (audio, transcription, a bad link) fails at once.

Every episode is queued in one language, said in the reply ("Queued (position 1 of 1). Language:
English."): the one the user gives (--language; it becomes the show's language), else the show's
confirmed language, else one detected with confidence (the source's own language tag and the title and
description agree, or with no tag the title and the description each say it: "Language: <language>
(detected: …)"; it stays the episode's, not the show's), else English. A link from a show without a
confirmed language that may be in another language, on weaker or conflicting evidence, is not queued:
the reply asks which language it is in.

One episode is often published on both Apple Podcasts and YouTube. A link whose episode is already
queued or in the library under the other source's link is not queued (the show monitor passes the
show, title and publish time it read from the feed, so this costs no lookup); podnote itself also
stops before any download when the show's folder already holds a note for the episode.

Files, all in PODNOTE_STATE:
    app.sqlite3       the queue, who asked for what, and the messages still to send (store.py): one
                      transaction admits an episode with its request, one finishes it with its messages
    queue.txt         the queue's links in order, and queue-state.json their bookkeeping: read-only
                      exports, rewritten after each change (edit with remove / move, not by hand)
    done.txt          when, link, status, note
    failed.txt        when, link, reason

Messages to members (and the admin's notices) go through the store's outbox: written in the same
transaction as the change they report, sent after it is committed, outside every lock, one at a time;
a send that fails or is cut off is tried again (a rare duplicate is accepted, a lost message is not).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402,F401  (importing it loads the project .env)
from files import atomic_write  # noqa: E402
import languages  # noqa: E402
import notify  # noqa: E402
import results  # noqa: E402  (what podnote reports)
import podshows  # noqa: E402
import sources  # noqa: E402
import store  # noqa: E402  (the queue, requests and outbox)
import users  # noqa: E402

STATE = Path(os.environ.get("PODNOTE_STATE", "/var/tmp/podnote-state"))
CACHE = Path(os.environ.get("PODNOTE_CACHE", "/var/tmp/podnote"))
KEEP_DAYS = int(os.environ.get("PODNOTE_KEEP_DAYS", "30"))   # an episode's transcript and working files
ROOT = Path(os.environ.get("PODNOTE_ROOT", str(Path.home() / "Notes")))
EPISODES = Path(os.environ.get("PODNOTE_NOTES_DIR", "Podcasts"))
PODNOTE = HERE / "podnote.py"
PODVERIFY = HERE / "podverify.py"
RETRY_HOURS = (1, 3, 6, 12)            # after the first failure; a fifth failure is final
RUN_TIMEOUT = 40 * 60                  # podnote + verification stay below Hermes' one-hour script limit
RESUMES = 6                            # runs a long episode may take on (40 minutes each) while each saves work
VERIFY_TIMEOUT = 15 * 60

# podnote marks a failure a later try may fix with a "RETRY: later" line; these STOPPED reasons count too:
# a run stopped by the time limit, and the model server's load and health errors from the ASR layer
RETRYABLE = re.compile(r"timed? ?out|model (load|unload)|oMLX health check", re.I)
BUSY = re.compile(r"another podnote run is active|ASR slot is already active", re.I)
VERIFY_OVERDUE = timedelta(hours=6)   # a verification retry this late takes its turn even when episodes wait
OUTBOX_WAITS = (5, 15, 60, 180)        # minutes before a failed send is tried again; after the last it is given up


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def read_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.is_file() else []


def append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


@contextmanager
def queue_db():
    """The store's write transaction. The files of before are imported on first use; after a change the
    read-only exports (queue.txt, queue-state.json) are rewritten before it commits."""
    with store.transaction() as db:
        if not store.meta(db, "imported"):
            import_files(db)
        before = db.total_changes
        yield db
        if db.total_changes != before:
            export(db)


def load_state() -> dict:
    with queue_db() as db:
        return store.queue(db)[1]


def export(db) -> None:
    links, entries = store.queue(db)
    atomic_write(STATE / "queue.txt", "\n".join(links) + ("\n" if links else ""))
    atomic_write(STATE / "queue-state.json", json.dumps(entries, indent=2, sort_keys=True) + "\n")


def import_files(db) -> str:
    """Once: the queue, its bookkeeping and the request log as they were before the store, kept as
    *.before-store copies. The store must hold exactly what the files held, or nothing is imported."""
    links = read_lines(STATE / "queue.txt")
    path = STATE / "queue-state.json"
    entries = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    log = users.STATE / "user-requests.log"
    asked = [line.split("\t") for line in (log.read_text(encoding="utf-8").splitlines() if log.is_file() else [])]
    asked = [fields for fields in asked if len(fields) >= 3]
    store.save_queue(db, links, {link: entries.get(link, {}) for link in links})
    for fields in asked:
        store.record_request(db, fields[1], fields[2], fields[3:4] == ["joined"], at=fields[0])
    held = store.queue(db)
    if held[0] != links or len(store.requests(db)) != len(asked):
        raise RuntimeError("store import does not match the files; nothing imported")
    for old in (STATE / "queue.txt", path, log):
        if old.is_file():
            atomic_write(old.with_name(old.name + ".before-store"), old.read_text(encoding="utf-8"))
    summary = f"{store.utc_now()} {len(links)} queued, {len(asked)} requests"
    store.meta(db, "imported", summary)
    return summary


def episode_id(url: str) -> str:
    """The episode's job id ("podcast-<Apple id>", "youtube-<video id>"), or ValueError if the link is
    not an episode link of a supported source."""
    return sources.episode_ref(url).job_id


def in_library(ref: sources.Ref) -> Path | None:
    """The note already made for this episode id in the podcasts folder (any show's folder), if any."""
    folder = ROOT / EPISODES
    marker = re.compile(rf"(?m)^{ref.id_field}: ['\"]?{re.escape(ref.episode)}['\"]?\s*$")   # any quoting
    return next((path for path in sorted(folder.rglob("*.md")) if folder.is_dir()
                 and marker.search(path.read_text(encoding="utf-8", errors="replace")[:4000])), None)


def joined(person: str, user: dict, url: str, ref: sources.Ref, peek: dict | None, made: Path | None) -> str:
    """Someone asks for an episode already queued (url: the link being made) or already made (made: its
    note). The request is logged, and not counted toward their daily limit. A member is sent a made note's
    message at once and the admin is told; what to add to the answer is returned."""
    users.record_request(person, url, joined=True)
    if users.is_admin(user):
        return ""
    if made:
        tell(person, note_message(url, str(made)))
        tell_admin(f"👤 {user['name']} asked for {what_episode(ref, peek)} (made already; sent)")
        return " Its summary is on its way to you."
    tell_admin(f"👤 {user['name']} asked for {what_episode(ref, peek)} (queued already)")
    return " You will get its note when it is made."


def tell(person: str, text: str) -> None:
    """A message for one person, into the outbox (deliver sends it once the change is committed)."""
    with store.transaction() as db:
        store.send_later(db, person, text)


def tell_admin(text: str) -> None:
    tell(users.admin()[0], text)


def deliver() -> int:
    """Send the outbox's due messages one at a time, outside every lock; returns how many went out. A
    send cut off mid-way (a crash) counts as not sent after 10 minutes and is tried again; a failed one
    is tried again after OUTBOX_WAITS, then given up (logged in notify.log, shown by `list`)."""
    sent = 0
    while True:
        with store.transaction() as db:
            db.execute("UPDATE outbox SET state = 'pending', error = 'cut off mid-send' WHERE state = 'sending' "
                       "AND next_try < ?", (iso(now() - timedelta(minutes=10)),))
            row = db.execute("SELECT * FROM outbox WHERE state = 'pending' AND next_try <= ? ORDER BY id LIMIT 1",
                             (iso(now()),)).fetchone()
            if row is None:
                return sent
            db.execute("UPDATE outbox SET state = 'sending', attempts = attempts + 1, next_try = ? WHERE id = ?",
                       (iso(now()), row["id"]))
        outcome = notify.deliver(row["person"], row["text"])
        tries = row["attempts"] + 1
        with store.transaction() as db:
            if outcome in ("sent", "skipped"):
                db.execute("UPDATE outbox SET state = ?, done = ? WHERE id = ?", (outcome, iso(now()), row["id"]))
                sent += outcome == "sent"
            elif tries > len(OUTBOX_WAITS):
                db.execute("UPDATE outbox SET state = 'failed', error = ?, done = ? WHERE id = ?",
                           (outcome, iso(now()), row["id"]))
            else:
                db.execute("UPDATE outbox SET state = 'pending', error = ?, next_try = ? WHERE id = ?",
                           (outcome, iso(now() + timedelta(minutes=OUTBOX_WAITS[tries - 1])), row["id"]))


def add(url: str, source: str = "", peek: dict | None = None, language: str | None = None,
        force: bool = False) -> str:
    try:
        return admit(url, source, peek, language, force)
    finally:
        deliver()                                       # its notices, once admitted


def admit(url: str, source: str = "", peek: dict | None = None, language: str | None = None,
          force: bool = False) -> str:
    """Queue an episode link. peek: {show (registry key), title, published, seconds, description, language_hint}
    when the caller already knows them (the monitor, from the feed), so the same episode under another
    source's link is recognised in the queue and the library and its language can be judged; for a link
    sent by hand they are looked up (lookup_peek). language: what the user says the episode is in.
    force: queue it even though the library or the queue seems to hold it already (show, title and
    time are a good sign of one episode on two sources, not proof); podnote then makes it regardless."""
    url = url.strip().strip("<>")
    try:
        ref = sources.episode_ref(url)
    except ValueError as error:
        return f"Not queued: {error}."
    url = ref.url                                       # the clean link: no share or tracking parameters ("?si=…")
    said = languages.code(language) if language else None
    if language and not said:
        return f"Not queued: {language.strip()!r} is not a language I know (give its English name or its ISO code)."
    if ref.source == "youtube":
        try:
            sources.youtube.ytdlp_program()
        except sources.SourceError as error:
            return f"Not queued: {error}."
    person, user = "", {}
    if source != "monitor":                             # someone in a chat (or the admin at a shell)
        person, user, new = users.current()
        if new:
            tell_admin(f"👤 New user: {user['name']}")
        if user.get("role") == "blocked":
            return "Not queued: not available."
    member = bool(person) and not users.is_admin(user)
    if peek is None and source != "monitor":
        peek = lookup_peek(ref, said)
    if peek:
        peek = {**peek, "source": ref.source, "id": ref.episode}   # this link's own source and id, whoever built it
    episode = ref.job_id
    answer, wanted, made = "", "", None                 # wanted: the queued link the asker joins; made: its note
    with queue_db() as db:
        queue, state = store.queue(db)
        queued = next((line for line in queue if episode_id_or_none(line) == episode), "")
        own = None if force else in_library(ref)
        found = "" if force or queued or own else twin(peek, queue, state)
        if queued:
            answer, wanted = f"Already queued (position {queue.index(queued) + 1} of {len(queue)}).", queued
        elif own:
            answer, wanted, made = "Already in the library; not queued.", url, own
        elif isinstance(found, Path):
            answer, wanted, made = ("Already in the library; not queued (from another source: add --force to "
                                    "make this one too)."), url, found
        elif found:
            answer, wanted = f"Already queued (position {queue.index(found) + 1} of {len(queue)}, from another source).", found
        else:
            spoken, how = episode_language(ref, peek, said)
            if spoken is None:
                return how                              # the question for the user
            if member and users.requests_today(person) >= users.limit(user):
                return f"Not queued: {users.limit(user)} episodes a day is the limit; try again tomorrow."
            queue.append(url)
            state[url] = {"added": iso(now()), "source": source or "manual", "attempts": 0,
                          "language": spoken, "language_source": how}
            if peek:
                state[url]["peek"] = peek
            if force:
                state[url]["force"] = True
            store.save_queue(db, queue, state)
            if person:                                  # in the same transaction: counted at once
                store.record_request(db, person, url)
        if answer:                                      # already queued or made: the asker joins it, in this
            return answer + (joined(person, user, wanted, ref, peek, made) if person else "")   # transaction
    if member:
        tell_admin(f"👤 {user['name']} queued {what_episode(ref, peek)}")
    if how == "user":                                   # a member's answer never overrides the admin's
        remember_language(show_of(ref, peek), spoken,
                          only_if_unset=member)
    return f"Queued (position {len(queue)} of {len(queue)}). Language: {languages.name(spoken)}."   # how: in the state


def episode_language(ref: sources.Ref, peek: dict | None, said: str | None) -> tuple[str | None, str]:
    """(language code, how it was chosen: "user", "show", "detected" or "default"), or (None, the
    question to ask) when the episode may be in another language than English and the evidence does
    not settle which (languages.judge: detected only when the source's tag and the text agree)."""
    show, peek = show_of(ref, peek), peek or {}
    decided = languages.decide(said, podshows.language_of(show) if show else "", None, peek.get("language_hint", ""),
                               peek.get("title", ""), peek.get("description", ""))
    if decided.code is None:
        looks = languages.name(decided.looks)
        return None, (f"Not queued yet: this episode looks {looks} (from {decided.basis}). "
                      f"Which language is it in? Reply with it, for example \"{looks}\" or \"English\".")
    return decided.code, decided.how


def show_of(ref: sources.Ref, peek: dict | None) -> str:
    """The registry key of a link's show, as far as the queue knows it: the peek's (any source), else an
    Apple link's own show id; "" for a YouTube link not looked up."""
    return (peek or {}).get("show") or (ref.show if ref.source == "apple" else "")


def twin_status(peek: dict | None) -> str:
    """Where this episode already is from another source: "library" (its note is made), "queue" (it waits
    or is being made: it may still fail), or ""."""
    with queue_db() as db:
        found = twin(peek, *store.queue(db))
    return "library" if isinstance(found, Path) else "queue" if found else ""


def what_episode(ref: sources.Ref, peek: dict | None) -> str:
    """'<show>: <title>' for a notice, as far as the queue knows them; the link otherwise."""
    peek = peek or {}
    show = (podshows.show_for(show_of(ref, peek)) or {}).get("folder", "")
    return f"{show}: {peek['title']}" if show and peek.get("title") else peek.get("title") or ref.url


def recipients(due: str, entry: dict) -> list[str]:
    """Who besides the admin gets a finished episode's message: everyone who asked for it, then the
    show's followers; the admin is left out (tick queues their copy itself, or prints it)."""
    import podnote
    keys = users.requesters(due)
    try:
        meta = json.loads((CACHE / "jobs" / sources.episode_ref(due).job_id / "source" / "resolved-episode-v1.json")
                          .read_text(encoding="utf-8"))
        keys += podshows.followers_of(podshows.show_for(podshows.show_key(meta)[0]) or {})
    except (OSError, ValueError):
        pass
    people = users.load()
    return [key for key in dict.fromkeys(keys) if key in people and not users.is_admin(people[key])]


def remember_language(show_id: str, code: str, only_if_unset: bool = False) -> None:
    """The language the user gave becomes the show's (when the registry knows the show already; a new
    show takes it from the episode's record when its first note is made). only_if_unset: a member's
    answer, which sets a show's language only when it has none."""
    if not show_id:
        return
    try:
        with podshows.editing() as registry:
            if show_id in registry and not (only_if_unset and registry[show_id].get("language")):
                podshows.set_language(registry[show_id], code)
    except Exception:
        pass                                            # the episode is queued with it either way


def lookup_peek(ref: sources.Ref, said: str | None = None) -> dict | None:
    """Show, title, publish time and description of a link sent by hand: for any YouTube video (one
    yt-dlp resolve), and for an Apple episode (one catalogue lookup) unless its show is not linked to
    YouTube and its language is settled. None when the source cannot say now: then podnote's own checks
    (twin, language) still apply before anything is downloaded."""
    show = (podshows.show_for(ref.show) or {}) if ref.source == "apple" else {}
    if ref.source == "apple" and not podshows.youtube_link(show, ref.show) and (said or show.get("language")):
        return None
    try:
        return ref.connector.peek(ref)
    except Exception:
        return None


def twin(peek: dict | None, queue: list[str], state: dict) -> Path | str:
    """The same episode from another source: its note when the show's folder has one, else its queued
    link, else "". The notes and the queue are one set of candidates, so two close episodes (one made,
    one waiting) leave the match in doubt; a queued link and its own fresh note count once."""
    if not peek or not peek.get("show"):
        return ""
    show = podshows.show_for(peek["show"]) or {}
    notes = sources.note_records(ROOT / EPISODES / show["folder"], peek["show"]) if show.get("folder") else []
    made = {(record["source"], record["id"]) for _, record in notes}
    queued = []
    for line in queue:
        try:
            ref = sources.parse(line)
        except ValueError:
            continue
        if (ref.source, ref.episode) not in made:
            queued.append((line, {**(state.get(line, {}).get("peek") or {}), "source": ref.source, "id": ref.episode}))
    return sources.twin_among(peek, notes + queued, show.get("title", "")) or ""


def source_of(line: str) -> str:
    try:
        return sources.parse(line).source
    except ValueError:
        return ""


def episode_id_or_none(url: str) -> str | None:
    try:
        return episode_id(url)
    except ValueError:
        return None


def log_done(line: str) -> None:
    """A line for done.txt, written once the finishing transaction commits: a rolled-back finish (the
    export failed) logs nothing, so its retry does not log the episode twice."""
    store.after_commit(lambda: append_line(STATE / "done.txt", line))


def finish(url: str, queue: list[str], state: dict) -> None:
    """The link leaves the queue (in the caller's transaction when there is one)."""
    queue.remove(url)
    state.pop(url, None)
    with queue_db() as db:
        store.save_queue(db, queue, state)


def run_owned(command: list[str], timeout: int) -> tuple[int, str] | None:
    """Run a command in its own process group; on timeout the whole group (its ASR and model requests
    included) is stopped. Returns (exit code, combined output), or None after a timeout."""
    import signal
    worker = {name: value for name, value in os.environ.items() if not name.startswith("HERMES_SESSION_")}
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               errors="replace", start_new_session=True, env=worker)   # the queue's work, not a chat's
    try:
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output or ""
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        return None


def saved_work(url: str) -> tuple[int, int]:
    """(files, bytes) in the episode's job folder: what a stopped run leaves for the next one to resume."""
    try:
        root = CACHE / "jobs" / sources.episode_ref(url).job_id
    except ValueError:
        return 0, 0
    files = [path for path in root.rglob("*") if path.is_file()] if root.is_dir() else []
    return len(files), sum(path.stat().st_size for path in files)


def run_podnote(url: str, language: str = "", source: str = "", force: bool = False) -> tuple[int, str]:
    """One podnote run; returns (exit code, combined output). A hung run counts as a timeout. language and
    source: the language the episode was queued in and who decided it (user, show, detected, default), so
    podnote makes it in the language the user was told; without them podnote decides itself."""
    done = run_owned([sys.executable, str(PODNOTE), url,
                      *(["--language", language, "--language-source", source or "user"] if language else []),
                      *(["--force"] if force else [])], RUN_TIMEOUT)
    return done if done is not None else (1, f"STOPPED: podnote timed out after {RUN_TIMEOUT // 60} minutes")


def release_models() -> None:
    """After a podcast job died mid-run, unload what it left loaded on the model server (only then: other
    work on the server is not ours to unload)."""
    try:
        import model_arbiter
        model_arbiter.reconcile(CACHE / "model-arbiter")
    except Exception:
        pass                                            # a server we cannot reach has nothing loaded by us


def run_verify(note_path: str) -> str:
    """The external validation pass over a fresh note; returns a short status for the log. Never raises:
    the note is already delivered; a failed pass is retried later on its own (retry_verification)."""
    done = run_owned([sys.executable, str(PODVERIFY), note_path], VERIFY_TIMEOUT)
    if done is None:
        release_models()
        return "verification timed out"
    code, output = done
    if "already verified" in output:
        return "already verified"
    found = re.search(r"Verified: (\d+) changes, (\d+) claims", output)
    if code or not found:
        return "verification failed: " + (output.strip().splitlines() or ["no output"])[-1][:120]
    changes, claims = int(found.group(1)), int(found.group(2))
    return f"verified: {changes} fix{'es' if changes != 1 else ''}, {claims} claim{'s' if claims != 1 else ''} checked"


def verification_failed(status: str) -> bool:
    return status.startswith(("verification failed", "verification timed out"))


def schedule_verification(note_path: str, tries: int = 0) -> None:
    """Try a failed verification pass again later (RETRY_HOURS after its latest failed try), without remaking
    the note; after the last try it is logged in failed.txt. Never reported on Telegram."""
    path = STATE / "verify-retry.json"
    retries = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if tries >= len(RETRY_HOURS):
        retries.pop(note_path, None)
        append_line(STATE / "failed.txt", f"{iso(now())}\tverification\tgave up after {tries} retries\t{note_path}")
    else:
        retries[note_path] = {"tries": tries + 1, "next_try": iso(now() + timedelta(hours=RETRY_HOURS[tries]))}
    atomic_write(path, json.dumps(retries, indent=2, sort_keys=True) + "\n")


def verification_due(late: timedelta = timedelta(0)) -> str:
    """The note whose verification retry is due (by `late` or more), or ""."""
    path = STATE / "verify-retry.json"
    retries = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return next((note for note, entry in sorted(retries.items(), key=lambda item: item[1]["next_try"])
                 if parse_iso(entry["next_try"]) + late <= now()), "")


def retry_verification() -> None:
    """One due verification retry, when the queue has no episode to run."""
    path = STATE / "verify-retry.json"
    retries = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    due = verification_due()
    if not due:
        return
    if not Path(due).is_file():                         # re-made or removed since: nothing to verify
        retries.pop(due)
        atomic_write(path, json.dumps(retries, indent=2, sort_keys=True) + "\n")
        return
    result = run_verify(due)
    if verification_failed(result):
        schedule_verification(due, retries[due]["tries"])
        return
    retries.pop(due)
    atomic_write(path, json.dumps(retries, indent=2, sort_keys=True) + "\n")
    append_line(STATE / "done.txt", f"{iso(now())}\tverification retry\t{result}\t{Path(due).stem}")
    refresh_show(due)


def refresh_show(note_path: str) -> None:
    """Let the show registry learn from the verified note (its name corrections count as evidence): the
    show whose folder holds the note."""
    try:
        folder = Path(note_path).parent.name
        with podshows.editing() as registry:
            for key in [key for key, show in registry.items() if show.get("folder") == folder]:
                podshows.refresh(registry, key)
        podshows.render()
    except Exception:
        pass                                            # the registry is never worth a failed episode


def describe(url: str, note_path: str = "") -> tuple[str, str, str, str]:
    """(show folder, episode title, participants, quick summary) for the Telegram message: from the note
    when there is one, else from the episode's job record; the link itself as a last resort."""
    show = episode = people = mood = ""
    if note_path and Path(note_path).is_file():
        text = Path(note_path).read_text(encoding="utf-8", errors="replace")
        show = Path(note_path).parent.name
        found = re.search(r"(?m)^episode: (.+)$", text)
        if found:
            value = found.group(1).strip()
            try:
                episode = json.loads(value) if value.startswith('"') else value.strip("'")
            except json.JSONDecodeError:
                episode = value.strip("\"'")
        found = re.search(r"\*\*Participants:\*\*\s*(.+)", text)
        if found:
            people = re.sub(r"\*\*([^*]+)\*\*", r"\1", found.group(1)).strip().rstrip(".")
        mood = quick_summary(text)
    if not (show and episode):
        try:
            ref = sources.episode_ref(url)
            meta = json.loads((CACHE / "jobs" / ref.job_id
                               / "source" / "resolved-episode-v1.json").read_text(encoding="utf-8"))
            episode = episode or str(meta.get("episode") or "")
            import podnote
            show = show or (podshows.show_for(podshows.show_key(meta)[0]) or {}).get("folder") or podshows.folder_name(str(meta.get("show") or ""))
        except Exception:
            pass
    return show or "Podcast", episode or url, people, mood


def key_themes(note: str, most: int = 8) -> list[tuple[str, list[str]]]:
    """The note's key themes in order: (title, the headers of its bullets). Titles are the "### "
    headings under "## Key themes"; a bullet's header is its bold lead ("- **Market pressures** — …")."""
    section = re.search(r"(?ms)^## Key themes\s*\n(.*?)(?=^## |\Z)", note)
    themes = []
    for block in re.split(r"(?m)^### +", section.group(1))[1:] if section else []:
        title, _, body = block.partition("\n")
        points = re.findall(r"(?m)^\s*[-*] +\*\*(.+?)\*\*", body)
        clean = lambda text: re.sub(r"[*_`]", "", text).strip().rstrip(":")
        themes.append((clean(title), [clean(point) for point in points]))
    return themes[:most]


def note_message(url: str, note_path: str) -> str:
    """The finished-note message for the episode at this link, from its note."""
    show, episode, people, mood = describe(url, note_path)
    text = Path(note_path).read_text(encoding="utf-8") if Path(note_path).is_file() else ""
    link = source_link(text) or (url if url.startswith("http") else "")
    return finished_message(show, episode, people, mood, key_themes(text), link)


def source_link(note: str) -> str:
    """The episode where the note was made from (its source_url: Apple Podcasts or YouTube), with only
    the query that names the episode (Apple's i=, YouTube's v=) kept."""
    found = re.search(r"(?m)^source_url: ['\"]?([^'\"\s]+)", note[:3000])
    if not found:
        return ""
    parts = urlsplit(found.group(1))
    kept = [(key, value) for key, value in parse_qsl(parts.query) if key in ("i", "v")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def finished_message(show: str, episode: str, people: str, mood: str, themes: list, link: str = "") -> str:
    """The chat message for a finished note: show and episode (bold), who took part (italic), the
    one-sentence take as a quote, the episode's link, and the key themes, each with the headers of its
    points. Standard Markdown limited to what Telegram and WhatsApp both show (bold, italic, a quote, a
    bare link); notify turns it into WhatsApp's own marks. The language is left out: a follower knows it."""
    plain = lambda text: " ".join(re.sub(r"[*`]", "", text).split()).replace("](", "] (")   # one line, no marks or links
    lines = [f"🟢 **{plain(show)}: {plain(episode)}**"]
    lines += [f"*{plain(people)}*"] if people else []
    lines += [f"> {plain(mood)}"] if mood else []
    lines += [link] if link else []
    if themes:
        lines += ["", "**Key themes**"]
        for theme in themes:
            title, points = theme if isinstance(theme, tuple) else (theme, [])
            lines.append(f"• **{plain(title)}**")
            lines += [f"   – {plain(point)}" for point in points]
    return "\n".join(lines)


def quick_summary(note: str) -> str:
    """The one-sentence quick summary under the Participants line of the note's summary callout. (A note
    made before it existed carries a tone line there instead: then the paragraph's first sentence.)"""
    callout = re.search(r"(?ms)^> \[!abstract\][^\n]*\n((?:>.*\n?)+)", note)
    if not callout:
        return ""
    paragraphs = [p.strip() for p in re.split(r"\n>\s*\n", "\n".join(line[1:].strip() if line.startswith(">") else line
                                                                    for line in callout.group(1).splitlines()))]
    paragraphs = [" ".join(p.split()) for p in "\n\n".join(paragraphs).split("\n\n") if p.strip()]
    rest = [p for p in paragraphs if not p.startswith("**Participants:**")]
    if rest and rest[0].startswith("**Sentiment:**"):
        rest = [re.split(r"(?<=[.!?])\s", rest[1], maxsplit=1)[0]] if len(rest) > 1 else []
    return rest[0] if rest else ""


PLAIN_REASONS = [                                   # the failure in plain words; the log keeps the detail
    (r"^(?:YouTube|yt-dlp)\b[^;]*", lambda m: m.group(0).strip().rstrip(".") + "."),   # already plain
    (r"episode looks (\w+)", lambda m: f"It looks {m.group(1)}, and its language was not confirmed: send the link "
                                        f"again and say which language it is in."),
    (r"not (?:been )?indexed|was not returned for show", lambda m: "Apple has not listed this episode yet."),
    (r"timed out after (\d+) minutes", lambda m: f"It took longer than {m.group(1)} minutes and was stopped."),
    (r"ffprobe|audio (?:file )?(?:could not|cannot)|rejected audio", lambda m: "The audio file could not be read."),
    (r"model server|unreachable|connection refused|oMLX", lambda m: "The model server on the Mac could not be reached."),
    (r"note incomplete: (.+?): length", lambda m: f"The model ran out of room writing {m.group(1)}."),
    (r"note incomplete: (.+)", lambda m: f"Part of the note failed: {m.group(1)}."),
    (r"metadata check: (.+)", lambda m: f"The note failed the metadata check ({m.group(1)})."),
    (r"No module named '([^']+)'", lambda m: f"Hermes is missing the Python package {m.group(1)}."),
    (r"[Aa]udio (?:download|server)[^;]*", lambda m: f"The audio could not be downloaded ({m.group(0).strip()})."),
    (r"feed|RSS|HTTP (\d{3})", lambda m: "The podcast's feed could not be read."),
]


def plain_reason(reason: str) -> str:
    """A failure reason in one plain sentence."""
    for pattern, say in PLAIN_REASONS:
        found = re.search(pattern, reason, re.I)
        if found:
            return say(found)
    return reason.strip().splitlines()[-1][:160] if reason.strip() else "It failed without saying why."


def clean_cache(queue: list[str], today: datetime | None = None) -> str:
    """Keep the cache small, at most once a day: an episode's audio goes once its transcript is
    complete, and its whole working folder (transcript, part notes, records) once it has not been
    used for KEEP_DAYS (transcribed, written or verified). Episodes in the queue are left alone; the
    notes are never touched. Returns a one-line summary for the log ("" when it did not run)."""
    today = today or now()
    stamp = STATE / "cache-cleaned"
    if stamp.is_file() and today - parse_iso(stamp.read_text().strip()) < timedelta(days=1):
        return ""
    import shutil
    import podnote
    queued = {job for job in map(episode_id_or_none, queue) if job}
    removed, freed = 0, 0
    jobs = sorted(job for prefix in sources.job_prefixes() for job in (CACHE / "jobs").glob(prefix + "*")) \
        if (CACHE / "jobs").is_dir() else []
    for job in jobs:
        if job.name in queued or not job.is_dir():
            continue
        used = [p.stat().st_mtime for p in (job / "asr" / "transcript-v1.json", job / "note-record.json",
                                            job / "verify-record.json") if p.is_file()] or [job.stat().st_mtime]
        if today - datetime.fromtimestamp(max(used), timezone.utc) > timedelta(days=KEEP_DAYS):
            freed += sum(p.stat().st_size for p in job.rglob("*") if p.is_file())
            shutil.rmtree(job, ignore_errors=True)
            removed += 1
        else:
            freed += podnote.drop_audio(job)
    atomic_write(stamp, iso(today) + "\n")
    line = f"{iso(today)}\tremoved {removed} episode folders older than {KEEP_DAYS} days\tfreed {freed / 1e9:.1f} GB"
    append_line(STATE / "cache-cleanup.log", line)
    return line


def run() -> str:
    """Process the next due link. Returns the admin's message (the cron job's output), or "" for nothing
    to report. Messages are sent after its transaction is committed (deliver): the members', and a
    finished note's for an admin with a delivery address."""
    try:
        return tick()
    finally:
        deliver()


def tick() -> str:
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "queue-run.lock").open("a+") as runner:
        try:
            fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ""                                   # the previous tick is still working
        with queue_db() as db:
            queue, state = store.queue(db)
        try:
            clean_cache(queue)                          # silent: it only writes cache-cleanup.log
        except OSError:
            pass                                        # a cleanup problem never stops the queue
        with queue_db() as db:
            queue, state = store.queue(db)
            due = next((url for url in queue
                        if parse_iso(state.get(url, {}).get("next_try", "1970-01-01T00:00:00Z")) <= now()), None)
        if due is None or verification_due(VERIFY_OVERDUE):   # idle, or a retry long overdue: its turn
            retry_verification()
            return ""

        entry = state.get(due, {})
        before = saved_work(due)
        code, output = run_podnote(due, entry.get("language", ""), entry.get("language_source", ""),
                                   force=bool(entry.get("force")))
        found = results.parse(output)
        note_path = found.get("NOTE", "")
        note = Path(note_path).stem if note_path else ""
        status = found.get("STATUS", "")
        stopped = found.get("STOPPED", "")
        if stopped.startswith("podnote timed out") or (code and not stopped):
            release_models()                            # it died mid-run: unload what it left on the server
        qa_status = run_verify(note_path) if note_path else ""
        if verification_failed(qa_status):
            schedule_verification(note_path)
        if note_path:
            refresh_show(note_path)

        with queue_db() as db:
            queue, state = store.queue(db)
            if due not in queue:                        # removed (podqueue.py remove) while it ran
                return ""
            entry = state.setdefault(due, {"added": iso(now()), "source": "manual", "attempts": 0})
            if note:                                    # complete or partial: delivered either way
                log_done(f"{iso(now())}\t{due}\t{status.split(' ')[0] or 'complete'}\t{note}")
                finish(due, queue, state)
                message = note_message(due, note_path)
                for key in recipients(due, entry):             # members: everyone who asked, the show's followers
                    store.send_later(db, key, message)
                admin, person = users.admin()
                if person.get("deliver"):                       # the admin's copy too, kept until it is sent;
                    store.send_later(db, admin, message)        # a set-up without one prints it (cron output)
                    return ""
                return message
            if "EXISTS" in found:                       # made already (found only now): those who asked get it
                log_done(f"{iso(now())}\t{due}\talready-in-library\t")
                existing = found["EXISTS"]
                askers = users.requesters(due)
                finish(due, queue, state)
                if existing and Path(existing).is_file():
                    message, people = note_message(due, existing), users.load()
                    for key in dict.fromkeys(askers):
                        if key in people and not users.is_admin(people[key]):
                            store.send_later(db, key, message)
                return ""
            if BUSY.search(stopped):                    # someone else is running podnote: just wait
                return ""
            reason = stopped or (output.strip().splitlines() or ["podnote failed without a message"])[-1][:300]
            if stopped.startswith("podnote timed out") and saved_work(due) != before \
                    and entry.get("resumes", 0) < RESUMES:
                entry["resumes"] = entry.get("resumes", 0) + 1     # a long episode: carry on next tick
                entry["next_try"] = iso(now())
                entry["last_reason"] = f"{reason}; its work so far is saved, carrying on (run {entry['resumes'] + 1})"
                store.save_queue(db, queue, state)
                return ""
            if (found.get("RETRY") == "later" or RETRYABLE.search(reason)) \
                    and entry.get("attempts", 0) < len(RETRY_HOURS):
                first = parse_iso(entry.setdefault("first_failure", iso(now())))
                entry["next_try"] = iso(first + timedelta(hours=RETRY_HOURS[entry.get("attempts", 0)]))
                entry["attempts"] = entry.get("attempts", 0) + 1
                entry["last_reason"] = reason
                store.save_queue(db, queue, state)
                return ""                               # retries are silent; only the final verdict is sent
            append_line(STATE / "failed.txt", f"{iso(now())}\t{due}\t{reason}")
            finish(due, queue, state)
            tries = entry.get("attempts", 0)
            show, episode, _, _ = describe(due)
            message = f"🔴 {show}: {episode}\n{plain_reason(reason)}" + (f" ({tries + 1} tries)" if tries else "")
            people = users.load()
            for key in users.requesters(due):          # a failure goes to everyone who asked, and the admin
                if key in people and not users.is_admin(people[key]):
                    store.send_later(db, key, message)
            return message


def listing() -> str:
    with queue_db() as db:
        queue, state = store.queue(db)
        waiting = db.execute("SELECT state, COUNT(*) AS n FROM outbox WHERE state IN ('pending', 'sending', 'failed') "
                             "GROUP BY state").fetchall()
    rows = []
    for position, url in enumerate(queue, 1):
        entry = state.get(url, {})
        retry = f" — retry {entry['attempts']} due {entry['next_try']} ({entry.get('last_reason', '')[:80]})" \
            if entry.get("next_try") else ""
        rows.append(f"{position}. {url}{retry}")
    messages = ", ".join(f"{row['n']} {row['state']}" for row in waiting)
    return "\n".join(rows or ["Queue is empty."]) + (f"\nMessages: {messages}." if messages else "")


def pick(queue: list[str], which: str) -> str:
    """A queued link by its position (1 = next) or the link itself."""
    if which.isdigit() and 1 <= int(which) <= len(queue):
        return queue[int(which) - 1]
    if which in queue:
        return which
    raise SystemExit(f"not in the queue: {which} (give a position from `list`, or the link)")


def remove(which: str) -> str:
    with queue_db() as db:
        queue, state = store.queue(db)
        link = pick(queue, which)
        finish(link, queue, state)
    return f"Removed: {link}"


def move(which: str, to: str) -> str:
    with queue_db() as db:
        queue, state = store.queue(db)
        link = pick(queue, which)
        queue.remove(link)
        queue.insert(max(0, min(len(queue), int(to) - 1)), link)
        store.save_queue(db, queue, state)
    return f"Moved to position {queue.index(link) + 1}: {link}"


def export_files() -> str:
    """The store written back as the files of before (queue.txt, queue-state.json, user-requests.log):
    what an earlier version of the skill reads after a rollback."""
    with queue_db() as db:
        export(db)
        asked = store.requests(db)
    atomic_write(users.STATE / "user-requests.log",
                 "".join(f"{at}\t{who}\t{link}" + ("\tjoined" if joined else "") + "\n" for at, who, link, joined in asked))
    return f"Exported {len(load_state())} queued and {len(asked)} requests to {STATE}."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    adding = sub.add_parser("add")
    adding.add_argument("url")
    adding.add_argument("--source", default="")
    adding.add_argument("--language", default=None, help="the language the episode is in (its English name or ISO 639-1 code)")
    adding.add_argument("--force", action="store_true", help="queue it even if it seems to be in the library or the queue")
    sub.add_parser("run")
    sub.add_parser("list")
    removing = sub.add_parser("remove", help="take a link out of the queue (admin)")
    removing.add_argument("which", help="its position in `list`, or the link")
    moving = sub.add_parser("move", help="put a queued link at another position (admin)")
    moving.add_argument("which")
    moving.add_argument("to", type=int)
    sub.add_parser("export", help="write the store back as the files of before, for a rollback (admin)")
    args = parser.parse_args()
    if args.command in ("remove", "move", "export", "run", None):   # run, or none: the worker (the cron job)
        refused = users.admin_only("run the queue" if args.command in ("run", None) else "change the queue by hand")
        if refused:
            print(refused)
            return 1
    if args.command == "add":
        if args.source == "monitor":                    # the monitor adds in-process; a chat never passes for it
            print("Not queued: --source monitor is the show monitor's own; leave --source out or use chat.")
            return 1
        print(add(args.url, args.source, language=args.language, force=args.force))
    elif args.command == "list":
        print(listing())
    elif args.command == "remove":
        print(remove(args.which))
    elif args.command == "move":
        print(move(args.which, args.to))
    elif args.command == "export":
        print(export_files())
    else:                                               # no command: the cron job's call
        message = run()
        if message:
            print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
