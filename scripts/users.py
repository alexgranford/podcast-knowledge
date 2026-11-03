"""users — the people who use the skill, by name, and who is asking right now.

Several people can use one installation through the agent: the admin (who owns the notes folder, the
model server and the settings) and members. Each person is one entry in PODNOTE_STATE/users.json under
a short key, with every identity they write from (a Telegram id, a WhatsApp number):

    {"robin": {"name": "Robin", "role": "member", "identities": ["whatsapp:<phone>"],
               "deliver": "whatsapp:<phone>", "limit_per_day": 10}}

Roles: admin, member, blocked. The file holds phone numbers and ids; it stays in the state folder,
mode 600, and nothing from it is written to a note.

Who is asking is read from the session environment Hermes gives every command its agent runs
(HERMES_SESSION_PLATFORM, HERMES_SESSION_USER_ID, HERMES_SESSION_USER_NAME), never from arguments. A
command run with no session (a shell, a cron job) is the admin's. Someone Hermes lets in who is not in
the file yet becomes a member on their first request, under the name their chat profile gives, and the
admin is told. Hermes, not the skill, decides who may talk to the agent at all (its allowlists and
pairing); authorized() asks the same question before anyone is sent a message.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from files import atomic_write

STATE = Path(os.environ.get("PODNOTE_STATE", "/var/tmp/podnote-state"))
HERMES_CODE = Path(os.environ.get("HERMES_CODE_DIR", "/opt/hermes"))     # Hermes's own modules, when installed
DAILY_LIMIT = int(os.environ.get("PODNOTE_USER_DAILY_LIMIT", "10"))      # episodes a member may queue a day
ROLES = ("admin", "member", "blocked")


def path() -> Path:
    return STATE / "users.json"


def load() -> dict:
    try:
        return json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@contextmanager
def editing():
    """The user list, locked for a read-change-write; written back (mode 600) when the block ends."""
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "users.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        users = load()
        yield users
        atomic_write(path(), json.dumps(users, ensure_ascii=False, indent=2) + "\n", mode=0o600)


def _hermes(module: str):
    """A module of the Hermes installation the skill runs beside, or None."""
    if HERMES_CODE.is_dir() and str(HERMES_CODE) not in sys.path:
        sys.path.append(str(HERMES_CODE))
    try:
        return __import__(module, fromlist=["_"])
    except Exception:
        return None


def normalize(identity: str) -> str:
    """'platform:id' with the id in one form: a WhatsApp phone number, phone JID or anonymous id (…@lid)
    all become the same number (through Hermes's own mapping when it is installed)."""
    platform, _, raw = str(identity or "").strip().partition(":")
    platform, raw = platform.lower(), raw.strip()
    if platform in ("whatsapp", "whatsapp_cloud"):
        identity_module = _hermes("gateway.whatsapp_identity")
        if identity_module is not None:
            try:
                raw = identity_module.canonical_whatsapp_identifier(raw) or raw
            except Exception:
                pass
        raw = re.sub(r"\D", "", raw.split("@")[0].split(":")[0])
        platform = "whatsapp"
    return f"{platform}:{raw}" if platform and raw else ""


def find(identity: str, users: dict | None = None) -> str:
    """The key of the person with this identity, or ""."""
    wanted = normalize(identity)
    users = load() if users is None else users
    return next((key for key, user in users.items() if wanted and wanted in {normalize(i) for i in user.get("identities", [])}), "")


def admin(users: dict | None = None) -> tuple[str, dict]:
    users = load() if users is None else users
    return next(((key, user) for key, user in users.items() if user.get("role") == "admin"), ("admin", {"name": "admin", "role": "admin"}))


def session_identity() -> tuple[str, str]:
    """('platform:user id', profile name) of the person the agent is answering, or ('', '') outside a chat."""
    platform = os.environ.get("HERMES_SESSION_PLATFORM", "").strip().lower()
    user = os.environ.get("HERMES_SESSION_USER_ID", "").strip() or os.environ.get("HERMES_SESSION_CHAT_ID", "").strip()
    if not platform or not user or platform in ("cli", "local", "cron"):
        return "", ""
    return normalize(f"{platform}:{user}"), os.environ.get("HERMES_SESSION_USER_NAME", "").strip()


def current() -> tuple[str, dict, bool]:
    """(key, entry, new) of the person asking. Outside a chat session: the admin. Someone not in the list
    yet is added as a member (new is True: the caller tells the admin)."""
    identity, profile = session_identity()
    if not identity:
        key, user = admin()
        return key, user, False
    with editing() as users:
        key = find(identity, users)
        if key:
            return key, users[key], False
        name = profile or identity.split(":", 1)[0].capitalize() + " user"
        key = base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "user"
        number = 2
        while key in users:
            key, number = f"{base}-{number}", number + 1
        users[key] = {"name": name, "role": "member", "identities": [identity], "deliver": identity,
                      "added": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        return key, users[key], True


def pick(everyone: dict, text: str) -> str:
    """The key of the person a name or key means; SystemExit when it names nobody or several."""
    wanted = text.strip().lower()
    found = [key for key, user in everyone.items() if wanted in (key, str(user.get("name", "")).lower())]
    if len(found) != 1:
        raise SystemExit(f"{'nobody' if not found else 'several people'} called {text.strip()!r}")
    return found[0]


def is_admin(user: dict) -> bool:
    return user.get("role") == "admin"


def admin_only(action: str) -> str:
    """'' when the person asking may do this: the admin, or no chat session at all (the queue, cron, a
    shell). Otherwise the refusal to print."""
    user = current()[1]
    return "" if is_admin(user) else f"Only {admin()[1].get('name', 'the admin')} can {action}."


def hermes_setting(name: str) -> str:
    """A setting from Hermes's own .env. Hermes keeps its allowlists out of the commands it runs, so the
    skill reads them where the gateway does ($HERMES_HOME/.env, in Docker /opt/data/.env)."""
    if os.environ.get(name):
        return os.environ[name]
    homes = [Path(os.environ["HERMES_HOME"])] if os.environ.get("HERMES_HOME") else []
    for env_file in [home / ".env" for home in homes + [Path.home() / ".hermes", Path("/opt/data")]]:
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                if key.strip() == name:
                    return value.strip().strip("'\"")
        except OSError:
            continue
    return ""


def authorized(target: str) -> bool:
    """Whether Hermes lets this identity in: its platform's allowlist (<PLATFORM>_ALLOWED_USERS) or an
    approved pairing. Nobody else is ever sent a message."""
    identity = normalize(target)
    if not identity:
        return False
    platform, user = identity.split(":", 1)
    allowed = hermes_setting(f"{platform.upper()}_ALLOWED_USERS")
    if any(normalize(f"{platform}:{entry}") == identity for entry in allowed.split(",") if entry.strip()):
        return True
    pairing = _hermes("gateway.pairing")
    if pairing is not None:
        try:
            return bool(pairing.PairingStore().is_approved(platform, user))
        except Exception:
            return False
    return False


def requests() -> list[tuple[str, str, str, bool]]:
    """Every request, oldest first: (when, person, episode link, joined). A request is a person asking for
    an episode; joined: it was already queued or made when they asked. Kept in the queue's store."""
    import store
    with store.transaction() as db:
        return store.requests(db)


def requests_today(key: str) -> int:
    """New episodes this person queued in the last 24 hours. Asking for one already queued or made does
    not count: it costs nothing to make."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    count = 0
    for when, who, _, joined in requests():
        try:
            if who == key and not joined and datetime.fromisoformat(when.replace("Z", "+00:00")) >= since:
                count += 1
        except ValueError:
            continue
    return count


def requesters(url: str) -> list[str]:
    """Everyone who asked for the episode at this link, the first to ask first."""
    return list(dict.fromkeys(who for _, who, link, _ in requests() if link == url))


def record_request(key: str, url: str, joined: bool = False) -> None:
    """Person `key` asked for the episode at `url`, the link it is (or will be) made from. joined: it
    was already queued or made. Inside the caller's store transaction when there is one."""
    import store
    with store.transaction() as db:
        store.record_request(db, key, url, joined)


def limit(user: dict) -> int:
    value = user.get("limit_per_day")
    return DAILY_LIMIT if value in (None, "") else int(value)
