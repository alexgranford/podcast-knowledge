#!/usr/bin/env python3
"""check_hermes — run after every Hermes upgrade: does Hermes still offer what this skill relies on?

The skill touches Hermes in two modules only: users (who is asking, who Hermes lets in, one form of a
WhatsApp number) and notify (sending a message). Each check below names what would break, so an upgrade
that moved something is found before a member's message silently goes missing:

- `hermes send --to <target> --quiet <text>` still exists (notify);
- gateway.whatsapp_identity.canonical_whatsapp_identifier and gateway.pairing.PairingStore.is_approved
  still import (users: without them numbers stop matching and approved pairings are not seen);
- the session variables HERMES_SESSION_PLATFORM / _USER_ID are still set by the gateway (users.current);
- WhatsApp text sent from outside the gateway still goes out raw, so notify's own Markdown conversion is
  still wanted (if Hermes starts converting there too, bold would turn into italics).

Prints one line per check and exits 1 when any fails. Reads only; sends nothing.
"""
from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify  # noqa: E402
import users  # noqa: E402


def send_options() -> str:
    done = subprocess.run([notify.hermes_program(), "send", "--help"], capture_output=True, text=True, timeout=60)
    missing = [flag for flag in ("--to", "--quiet") if flag not in done.stdout + done.stderr]
    return f"hermes send lacks {', '.join(missing)}" if missing or done.returncode else ""


def identity_modules() -> str:
    whatsapp, pairing = users._hermes("gateway.whatsapp_identity"), users._hermes("gateway.pairing")
    problems = []
    if not callable(getattr(whatsapp, "canonical_whatsapp_identifier", None)):
        problems.append("gateway.whatsapp_identity.canonical_whatsapp_identifier")
    if not callable(getattr(getattr(pairing, "PairingStore", None), "is_approved", None)):
        problems.append("gateway.pairing.PairingStore.is_approved")
    return "missing: " + ", ".join(problems) if problems else ""


def session_variables() -> str:
    code = users.HERMES_CODE / "gateway"
    text = "".join(path.read_text(encoding="utf-8", errors="replace") for path in code.rglob("*.py")) if code.is_dir() else ""
    missing = [name for name in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_USER_ID") if name not in text]
    return f"the gateway no longer mentions {', '.join(missing)}" if missing else ""


def whatsapp_raw() -> str:
    adapter = users._hermes("plugins.platforms.whatsapp.adapter")
    sender = getattr(adapter, "_standalone_send", None)
    if sender is None:
        return "plugins.platforms.whatsapp.adapter._standalone_send not found: check how hermes send reaches WhatsApp"
    return ("WhatsApp sends from outside the gateway are now formatted by Hermes: drop notify.whatsapp_text"
            if "format_message" in inspect.getsource(sender) else "")


def main() -> int:
    failed = 0
    for name, check in (("hermes send", send_options), ("identity modules", identity_modules),
                        ("session variables", session_variables), ("WhatsApp raw text", whatsapp_raw)):
        try:
            problem = check()
        except Exception as error:                      # a check that cannot run is a failed check
            problem = f"{type(error).__name__}: {error}"
        failed += bool(problem)
        print(f"{'FAIL' if problem else 'ok  '} {name}{': ' + problem if problem else ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
