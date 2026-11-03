"""notify — one chat message to one person, through Hermes (`hermes send`).

The queue and the monitor print the admin's messages, which their cron jobs deliver as before; this
module reaches everyone else: a member whose episode is finished or who follows its show, and the admin
when something happens in a member's chat (a new user, an episode queued, a show followed). A message
goes only to someone Hermes lets in (users.authorized), one person at a time, a few seconds apart on
WhatsApp (a personal-account bridge, not a broadcast service), with one retry. A failure is logged in
PODNOTE_STATE/notify.log (the person's name and the kind of failure: never an address or the
message) and never fails the caller.

Messages are written in standard Markdown, kept to what both apps show (bold, italic, a quote, a bare
link). Hermes converts it for Telegram on every route; for WhatsApp `hermes send` posts the text raw
(only the running gateway converts), so whatsapp_text does that here. Re-check after a Hermes upgrade:
if `hermes send` starts converting too, WhatsApp bold would come out as italics.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from datetime import datetime, timezone

import users

SPACING = {"whatsapp": 3.0}            # seconds between two messages on one platform
_last: dict[str, float] = {}


def hermes_program() -> str:
    return shutil.which("hermes") or "/opt/hermes/.venv/bin/hermes"


def log(line: str) -> None:
    users.STATE.mkdir(parents=True, exist_ok=True)
    with (users.STATE / "notify.log").open("a", encoding="utf-8") as stream:
        stream.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\t{line}\n")


def whatsapp_text(text: str) -> str:
    """Standard Markdown to WhatsApp's marks: *italic* -> _italic_, **bold** -> *bold*, ~~struck~~ ->
    ~struck~, [label](link) -> label (link). Code spans are left as they are."""
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", text)
    for index in range(0, len(parts), 2):
        part = re.sub(r"(?<!\*)\*(?!\s|\*)([^*\n]*?\S[^*\n]*?)\*(?!\*)", r"_\1_", parts[index])   # before bold
        part = re.sub(r"\*\*(.+?)\*\*", r"*\1*", part)
        part = re.sub(r"~~(.+?)~~", r"~\1~", part)
        parts[index] = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"\1 (\2)", part)
    return "".join(parts)


def failure(code: int, last: list[str], identity: str, text: str) -> str:
    """A failed send for the log: the exit code only. Hermes' own error text can carry the address or part
    of the message in any form, so none of it is kept."""
    return f"exit {code}"


def send(target: str, text: str, who: str = "") -> bool:
    """Send text to a Hermes delivery target ('whatsapp:<number>', 'telegram:<id>'). True when sent."""
    identity = users.normalize(target)
    who = who or identity.split(":", 1)[0]
    if not users.authorized(identity):
        log(f"skipped\t{who}\tnot let in by Hermes")
        return False
    platform = identity.split(":", 1)[0]
    if platform == "whatsapp":
        text = whatsapp_text(text)
    wait = SPACING.get(platform, 0) - (time.monotonic() - _last.get(platform, -1e9))
    if wait > 0:
        time.sleep(wait)
    for attempt in (1, 2):
        try:
            done = subprocess.run([hermes_program(), "send", "--to", identity, "--quiet", text],
                                  capture_output=True, text=True, errors="replace", timeout=60)
            ok = done.returncode == 0
            problem = [] if ok else [failure(done.returncode, (done.stderr or done.stdout).strip().splitlines()[-1:],
                                             identity, text)]
        except subprocess.TimeoutExpired:                 # its text holds the command: never logged
            ok, problem = False, ["timed out"]
        except OSError as error:
            ok, problem = False, [type(error).__name__]
        _last[platform] = time.monotonic()
        if ok:
            log(f"sent\t{who}")
            return True
        if attempt == 1:
            time.sleep(5)
    log(f"failed\t{who}\t{(problem or ['no message'])[0][:200]}")
    return False


def deliver(key: str, text: str, people: dict | None = None) -> str:
    """A message to the person with this key: "sent", "skipped" (nowhere to send it, or they are blocked:
    trying again would not help) or "failed" (worth another try; it may have gone out after all)."""
    people = users.load() if people is None else people
    person = people.get(key) or {}
    if not person.get("deliver") or person.get("role") == "blocked":
        return "skipped"
    return "sent" if send(person["deliver"], text, person.get("name") or key) else "failed"


def to_user(key: str, text: str, people: dict | None = None) -> bool:
    """A message to the person with this key, at their delivery target."""
    return deliver(key, text, people) == "sent"


def to_admin(text: str) -> bool:
    key, person = users.admin()
    return to_user(key, text) if person.get("deliver") else False
