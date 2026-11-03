#!/usr/bin/env python3
"""podstatus — how the pipeline is doing, read-only: the queue, the messages waiting to go out, the
validation passes waiting for a retry, and every watched source's health.

    podstatus.py              everything, one line per part
    podstatus.py --problems   only what needs attention (nothing printed, exit 0, when all is well)

A problem is something to act on: a source not read for STALE_HOURS, a message given up, one waiting
over an hour, a queued episode retried more than once, a verification retry overdue. It changes nothing
and runs nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import podmonitor  # noqa: E402
import podqueue  # noqa: E402
import podshows  # noqa: E402
import store  # noqa: E402
import users  # noqa: E402


def age(text: str) -> str:
    """'3 h' / '2 d' since an ISO time."""
    seconds = (podqueue.now() - podqueue.parse_iso(text)).total_seconds()
    return f"{seconds / 86400:.0f} d" if seconds >= 172800 else f"{seconds / 3600:.0f} h" if seconds >= 3600 else f"{seconds / 60:.0f} min"


def queue_part() -> tuple[str, list[str]]:
    with podqueue.queue_db() as db:
        links, entries = store.queue(db)
        messages = [dict(row) for row in db.execute(
            "SELECT person, state, created, error FROM outbox WHERE state IN ('pending', 'sending', 'failed')")]
    retrying = [(link, entries.get(link, {})) for link in links if entries.get(link, {}).get("attempts")]
    problems = [f"queue: {link} tried {entry['attempts']} times ({entry.get('last_reason', '')[:80]})"
                for link, entry in retrying if entry["attempts"] > 1]
    waiting = [m for m in messages if m["state"] != "failed"]
    late = [m for m in waiting if podqueue.now() - podqueue.parse_iso(m["created"]) > timedelta(hours=1)]
    problems += [f"message to {m['person']} given up ({m['error'] or 'failed'})" for m in messages if m["state"] == "failed"]
    problems += [f"message to {m['person']} waiting {age(m['created'])} ({m['error'] or 'not sent yet'})" for m in late]
    oldest = min((entries.get(link, {}).get("added", "") for link in links), default="")
    summary = (f"Queue: {len(links)} queued" + (f", {len(retrying)} retrying" if retrying else "")
               + (f", the oldest added {age(oldest)} ago" if oldest else "")
               + f". Messages: {len(waiting)} waiting, {len(messages) - len(waiting)} given up.")
    return summary, problems


def verification_part() -> tuple[str, list[str]]:
    path = podqueue.STATE / "verify-retry.json"
    retries = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    overdue = [note for note, entry in retries.items()
               if podqueue.parse_iso(entry["next_try"]) + podqueue.VERIFY_OVERDUE <= podqueue.now()]
    return (f"Verification: {len(retries)} waiting for a retry.",
            [f"verification of {Path(note).stem} overdue" for note in overdue])


def sources_part() -> tuple[str, list[str]]:
    path = podmonitor.STATE / "monitor-state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    problems, healthy = [], 0
    for key, entry in sorted(state.items()):
        if not isinstance(entry, dict) or "last_attempt" not in entry:
            continue
        failing = entry.get("failing_since")
        if failing and podqueue.now() - podqueue.parse_iso(failing) >= timedelta(hours=podmonitor.STALE_HOURS):
            show = (podshows.show_for(key) or {}).get("folder") or key
            problems.append(f"source {show}: not read for {age(failing)} ({entry.get('last_error', '')[21:120]})")
        else:
            healthy += 1
    return f"Sources: {healthy} read normally, {len(problems)} not.", problems


def runtime_part() -> tuple[str, list[str]]:
    """The speech-detection runtime: its readiness record matches the files it pins, or every episode fails."""
    from podcast_transcription import runtime_problem
    problem = runtime_problem()
    return ("Speech runtime: ready." if not problem else "Speech runtime: not ready.",
            [f"speech runtime: {problem}; run scripts/setup_vad.py"] if problem else [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--problems", action="store_true", help="only what needs attention")
    args = parser.parse_args()
    refused = users.admin_only("see the pipeline's status")
    if refused:
        print(refused)
        return 1
    parts = [queue_part(), verification_part(), sources_part(), runtime_part()]
    problems = [problem for _, found in parts for problem in found]
    if args.problems:
        print("\n".join(problems)) if problems else None
        return 1 if problems else 0
    print("\n".join([summary for summary, _ in parts] + [f"⚠️ {problem}" for problem in problems]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
