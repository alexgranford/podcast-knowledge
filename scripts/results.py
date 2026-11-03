"""results — what podnote prints for whoever runs it (the queue), as lines of their own: written with emit,
read with parse, so the caller never interprets sentences meant for people.

    NOTE: <path>        the note written (complete or partial)
    STATUS: <word>      how it ended: complete, partial
    STOPPED: <reason>   the run stopped and wrote nothing; the reason in plain words
    RETRY: later        that stop is one a later try may fix (a source or the model server away)
    EXISTS: <path>      the episode is already made: this note (from this source or another)

Anything else podnote prints is for people. A kind printed twice: the first one counts.
"""
from __future__ import annotations

import re

KINDS = ("NOTE", "STATUS", "STOPPED", "RETRY", "EXISTS")


def emit(kind: str, value: str) -> None:
    assert kind in KINDS, kind
    print(f"{kind}: {value}")


def parse(output: str) -> dict[str, str]:
    """{kind: value} for the result lines in a run's output."""
    found: dict[str, str] = {}
    for kind, value in re.findall(rf"(?m)^({'|'.join(KINDS)}):[ \t]*(.*?)\s*$", output):
        found.setdefault(kind, value)
    return found
