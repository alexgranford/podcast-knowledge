#!/usr/bin/env python3
"""podqueue — a plain queue of Apple Podcasts episode links in front of podnote.py.

    podqueue.py add <apple-episode-url> [--source NAME]   queue a link; prints one line
    podqueue.py run                                       process the next due link (the default)
    podqueue.py list                                      show the queue and any pending retries

podnote.py stays a single run; this file only decides which link it runs next. A Hermes cron job
(script mode, no model) calls `run` every few minutes: one episode per call, nothing printed when
there is nothing to report, one line per finished or failed episode otherwise (delivered to
Telegram by the cron job).

Retries only for an episode that was not processed - Apple has not indexed it yet, the model server
is unreachable, ASR has priority - at 1, 3, 6 and 12 hours after its first failure, then it is
marked failed. Another podnote run holding the lock is not a failure: the link simply waits. A
partial note is a failure (the old note stays). Anything else (audio, transcription, a bad link) fails at once.

Files, all in PODNOTE_STATE:
    queue.txt         bare links, one per line, in order (safe to edit by hand)
    queue-state.json  per-link retry bookkeeping
    done.txt          when, link, status, note
    failed.txt        when, link, reason
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402,F401  (importing it loads the project .env)
import podfetch  # noqa: E402

STATE = Path(os.environ.get("PODNOTE_STATE", "/var/tmp/podnote-state"))
ROOT = Path(os.environ.get("PODNOTE_ROOT", str(Path.home() / "Notes")))
EPISODES = Path(os.environ.get("PODNOTE_NOTES_DIR", "Podcasts"))
PODNOTE = HERE / "podnote.py"
PODVERIFY = HERE / "podverify.py"
RETRY_HOURS = (1, 3, 6, 12)            # after the first failure; a fifth failure is final
RUN_TIMEOUT = 40 * 60                  # podnote + verification stay below Hermes' one-hour script limit
VERIFY_TIMEOUT = 15 * 60

# STOPPED reasons that mean "not processed yet, try again later"
RETRYABLE = re.compile(r"not indexed|has not indexed|unreachable|network|timed? ?out|connection|"
                       r"ASR has priority|ASR slot is already active|model (load|unload)|oMLX health check|"
                       r"temporar", re.I)
BUSY = re.compile(r"another podnote run is active|ASR slot is already active", re.I)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.is_file() else []


def append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def load_state() -> dict:
    path = STATE / "queue-state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save_state(state: dict) -> None:
    atomic_write(STATE / "queue-state.json", json.dumps(state, indent=2, sort_keys=True) + "\n")


def episode_id(url: str) -> str:
    """The Apple episode id of a link, or ValueError if it is not an Apple episode link."""
    _, episode, _ = podfetch.parse_apple_url(url)
    if not episode:
        raise ValueError("this is a show link, not an episode link (it has no ?i=<episode-id>)")
    return episode


def in_library(episode: str) -> bool:
    """Is there already a note for this Apple episode id in the podcasts folder (any show's folder)?"""
    folder = ROOT / EPISODES
    marker = re.compile(rf"(?m)^apple_episode_id: ['\"]?{re.escape(episode)}['\"]?\s*$")   # any quoting
    return folder.is_dir() and any(marker.search(path.read_text(encoding="utf-8", errors="replace")[:4000])
                                   for path in folder.rglob("*.md"))


def queue_lock():
    STATE.mkdir(parents=True, exist_ok=True)
    stream = (STATE / "queue.lock").open("a+")
    fcntl.flock(stream, fcntl.LOCK_EX)
    return stream


def add(url: str, source: str = "") -> str:
    url = url.strip().strip("<>")
    try:
        episode = episode_id(url)
    except ValueError as error:
        return f"Not queued: {error}."
    with queue_lock():
        queue = read_lines(STATE / "queue.txt")
        if any(episode_id_or_none(line) == episode for line in queue):
            return f"Already queued (position {[episode_id_or_none(l) for l in queue].index(episode) + 1} of {len(queue)})."
        if in_library(episode):
            return "Already in the library; not queued."
        queue.append(url)
        atomic_write(STATE / "queue.txt", "\n".join(queue) + "\n")
        state = load_state()
        state[url] = {"added": iso(now()), "source": source or "manual", "attempts": 0}
        save_state(state)
        return f"Queued (position {len(queue)} of {len(queue)})."


def episode_id_or_none(url: str) -> str | None:
    try:
        return episode_id(url)
    except ValueError:
        return None


def finish(url: str, queue: list[str], state: dict) -> None:
    queue.remove(url)
    state.pop(url, None)
    atomic_write(STATE / "queue.txt", "\n".join(queue) + ("\n" if queue else ""))
    save_state(state)


def run_owned(command: list[str], timeout: int) -> tuple[int, str] | None:
    """Run a command in its own process group; on timeout the whole group (its ASR and model requests
    included) is stopped. Returns (exit code, combined output), or None after a timeout."""
    import signal
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               errors="replace", start_new_session=True)
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


def run_podnote(url: str) -> tuple[int, str]:
    """One podnote run; returns (exit code, combined output). A hung run counts as a timeout."""
    done = run_owned([sys.executable, str(PODNOTE), url], RUN_TIMEOUT)
    return done if done is not None else (1, f"STOPPED: podnote timed out after {RUN_TIMEOUT // 60} minutes")


def run_verify(note_path: str) -> str:
    """The external validation pass over a fresh note; returns a short status for the message.
    Never raises: the note is already delivered, so a verification problem is reported, not retried."""
    done = run_owned([sys.executable, str(PODVERIFY), note_path], VERIFY_TIMEOUT)
    if done is None:
        return "verification timed out"
    code, output = done
    if "already verified" in output:
        return "already verified"
    found = re.search(r"Verified: (\d+) changes, (\d+) claims", output)
    if code or not found:
        return "verification failed: " + (output.strip().splitlines() or ["no output"])[-1][:120]
    changes, claims = int(found.group(1)), int(found.group(2))
    return f"verified: {changes} fix{'es' if changes != 1 else ''}, {claims} claim{'s' if claims != 1 else ''} checked"


def refresh_show(note_path: str) -> None:
    """Let the show registry learn from the verified note (its name corrections count as evidence)."""
    try:
        import podshows
        found = re.search(r"(?m)^source_url: \S*?/id(\d+)", Path(note_path).read_text(encoding="utf-8")[:3000])
        if found:
            with podshows.editing() as registry:
                if found.group(1) in registry:
                    podshows.refresh(registry, found.group(1))
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
            show_id, episode_id, _ = podfetch.parse_apple_url(url)
            meta = json.loads((Path(os.environ.get("PODNOTE_CACHE", "/var/tmp/podnote")) / "jobs" / f"podcast-{episode_id}"
                               / "source" / "resolved-episode-v1.json").read_text(encoding="utf-8"))
            episode = episode or str(meta.get("episode") or "")
            import podshows
            show = show or (podshows.show_for(show_id) or {}).get("folder") or podshows.folder_name(str(meta.get("show") or ""))
        except Exception:
            pass
    return show or "Podcast", episode or url, people, mood


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
    (r"vad_boundary_unavailable", lambda m: "The audio could not be split for transcription (no pause to cut at)."),
    (r"not (?:been )?indexed|was not returned for show", lambda m: "Apple has not listed this episode yet."),
    (r"timed out after (\d+) minutes", lambda m: f"It took longer than {m.group(1)} minutes and was stopped."),
    (r"ffprobe|audio (?:file )?(?:could not|cannot)|rejected audio", lambda m: "The audio file could not be read."),
    (r"model server|unreachable|connection refused|oMLX", lambda m: "The model server on the Mac could not be reached."),
    (r"note incomplete: (.+?): length", lambda m: f"The model ran out of room writing {m.group(1)}."),
    (r"note incomplete: (.+)", lambda m: f"Part of the note failed: {m.group(1)}."),
    (r"metadata check: (.+)", lambda m: f"The note failed the metadata check ({m.group(1)})."),
    (r"No module named '([^']+)'", lambda m: f"Hermes is missing the Python package {m.group(1)}."),
    (r"feed|RSS|HTTP (\d{3})", lambda m: "The podcast's feed could not be read."),
]


def plain_reason(reason: str) -> str:
    """A failure reason in one plain sentence."""
    for pattern, say in PLAIN_REASONS:
        found = re.search(pattern, reason, re.I)
        if found:
            return say(found)
    return reason.strip().splitlines()[-1][:160] if reason.strip() else "It failed without saying why."


def line_after(prefix: str, output: str) -> str:
    match = re.search(rf"(?m)^{prefix}\s*(.*)$", output)
    return match.group(1).strip() if match else ""


def run() -> str:
    """Process the next due link. Returns the message to deliver, or "" for nothing to report."""
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "queue-run.lock").open("a+") as runner:
        try:
            fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ""                                   # the previous tick is still working
        with queue_lock():
            queue, state = read_lines(STATE / "queue.txt"), load_state()
            due = next((url for url in queue
                        if parse_iso(state.get(url, {}).get("next_try", "1970-01-01T00:00:00Z")) <= now()), None)
        if due is None:
            return ""

        code, output = run_podnote(due)
        note_path = line_after("NOTE:", output)
        note = Path(note_path).stem if note_path else ""
        status = line_after("STATUS:", output)
        stopped = line_after("STOPPED:", output)
        qa_status = run_verify(note_path) if note_path else ""
        if note_path:
            refresh_show(note_path)

        with queue_lock():
            queue, state = read_lines(STATE / "queue.txt"), load_state()
            if due not in queue:                        # edited out by hand while it ran
                return ""
            entry = state.setdefault(due, {"added": iso(now()), "source": "manual", "attempts": 0})
            if note:                                    # complete or partial: delivered either way
                append_line(STATE / "done.txt", f"{iso(now())}\t{due}\t{status.split(' ')[0] or 'complete'}\t{note}")
                finish(due, queue, state)
                show, episode, people, mood = describe(due, note_path)
                return "\n".join(line for line in (f"🟢 {show}: {episode}", people, mood) if line)
            if "note already exists" in output:
                append_line(STATE / "done.txt", f"{iso(now())}\t{due}\talready-in-library\t")
                finish(due, queue, state)
                return ""
            if BUSY.search(output):                     # someone else is running podnote: just wait
                return ""
            reason = stopped or (output.strip().splitlines() or ["podnote failed without a message"])[-1][:300]
            if RETRYABLE.search(reason) and entry.get("attempts", 0) < len(RETRY_HOURS):
                first = parse_iso(entry.setdefault("first_failure", iso(now())))
                entry["next_try"] = iso(first + timedelta(hours=RETRY_HOURS[entry.get("attempts", 0)]))
                entry["attempts"] = entry.get("attempts", 0) + 1
                entry["last_reason"] = reason
                save_state(state)
                return ""                               # retries are silent; only the final verdict is sent
            append_line(STATE / "failed.txt", f"{iso(now())}\t{due}\t{reason}")
            finish(due, queue, state)
            tries = entry.get("attempts", 0)
            show, episode, _, _ = describe(due)
            return f"🔴 {show}: {episode}\n{plain_reason(reason)}" + (f" ({tries + 1} tries)" if tries else "")


def listing() -> str:
    queue, state = read_lines(STATE / "queue.txt"), load_state()
    if not queue:
        return "Queue is empty."
    rows = []
    for position, url in enumerate(queue, 1):
        entry = state.get(url, {})
        retry = f" — retry {entry['attempts']} due {entry['next_try']} ({entry.get('last_reason', '')[:80]})" \
            if entry.get("next_try") else ""
        rows.append(f"{position}. {url}{retry}")
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    adding = sub.add_parser("add")
    adding.add_argument("url")
    adding.add_argument("--source", default="")
    sub.add_parser("run")
    sub.add_parser("list")
    args = parser.parse_args()
    if args.command == "add":
        print(add(args.url, args.source))
    elif args.command == "list":
        print(listing())
    else:                                               # no command: the cron job's call
        message = run()
        if message:
            print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
