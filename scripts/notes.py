"""notes — reading a note's properties and the notes' UTC form, without the note-making machinery
(podnote), so the show registry, the queue and the validation pass need not import it."""
from __future__ import annotations

import re
import time

import notecheck


def read_front(text: str) -> dict:
    """A note's properties; {} if there are none or they do not parse."""
    try:
        return notecheck.parse_note(text)[0] or {}
    except Exception:
        return {}



def utc(moment) -> str:
    """A datetime (aware, or naive meaning UTC) or an ISO string as the notes' UTC form
    YYYY-MM-DDTHH:MM:SS; "" when there is no instant (a date alone is not one)."""
    from datetime import datetime, timezone
    if isinstance(moment, str):
        text = moment.strip().strip("'\"")
        if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", text):
            return ""
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            return ""
    if not moment:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
