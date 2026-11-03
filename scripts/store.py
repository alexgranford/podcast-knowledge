"""store — the queue's own small database (SQLite, PODNOTE_STATE/app.sqlite3): what is queued, who asked
for what, and the messages still to send. One transaction admits an episode and records who asked for it;
one finishes an episode and queues its messages, so a crash can never leave them disagreeing.

    jobs      one row per queued link, in order (position); entry: its bookkeeping as JSON (added, source,
              attempts, language, language_source, peek, force, next_try, first_failure, resumes, last_reason)
    requests  a person asking for an episode (when, person, link, joined: it was queued or made already)
    outbox    a message for one person: pending, sending, sent, skipped or failed; attempts, next_try, error
    meta      key/value: when the files of before were imported

`transaction()` takes the write lock (BEGIN IMMEDIATE) and is re-entrant within a process, so a helper
called inside it joins it. Nothing slow (a lookup, a model run, a send) happens inside one.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import settings  # noqa: F401  (importing it loads the project .env: PODNOTE_STATE)

STATE = Path(os.environ.get("PODNOTE_STATE", "/var/tmp/podnote-state"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (link TEXT PRIMARY KEY, position INTEGER NOT NULL, entry TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, at TEXT NOT NULL, person TEXT NOT NULL,
                                     link TEXT NOT NULL, joined INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS requests_link ON requests (link);
CREATE INDEX IF NOT EXISTS requests_person ON requests (person, at);
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, created TEXT NOT NULL, person TEXT NOT NULL,
                                   text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                                   attempts INTEGER NOT NULL DEFAULT 0, next_try TEXT NOT NULL,
                                   error TEXT NOT NULL DEFAULT '', done TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS outbox_due ON outbox (state, next_try);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_local = threading.local()


def path() -> Path:
    return STATE / "app.sqlite3"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect() -> sqlite3.Connection:
    STATE.mkdir(parents=True, exist_ok=True)
    if not path().exists():                              # created private: SQLite gives its -wal and -shm
        os.close(os.open(path(), os.O_CREAT | os.O_WRONLY, 0o600))   # files the database file's mode
    db = sqlite3.connect(path(), timeout=60, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript(SCHEMA)
    for private in (path(), path().with_name(path().name + "-wal"), path().with_name(path().name + "-shm")):
        if private.exists():                             # it names people and holds their messages
            os.chmod(private, 0o600)
    return db


@contextmanager
def transaction():
    """The write lock and one atomic change; a nested call joins the outer one."""
    current = getattr(_local, "db", None)
    if current is not None and getattr(_local, "path", None) == path():
        yield current
        return
    db = _connect()
    _local.db, _local.path = db, path()
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        _local.db = _local.path = None
        db.close()


# ---- jobs

def queue(db) -> tuple[list[str], dict]:
    """(links in order, {link: entry}): the queue as podqueue works with it."""
    rows = db.execute("SELECT link, entry FROM jobs ORDER BY position").fetchall()
    return [row["link"] for row in rows], {row["link"]: json.loads(row["entry"]) for row in rows}


def save_queue(db, links: list[str], entries: dict) -> None:
    db.execute("DELETE FROM jobs")
    db.executemany("INSERT INTO jobs (link, position, entry) VALUES (?, ?, ?)",
                   [(link, number, json.dumps(entries.get(link, {}), sort_keys=True)) for number, link in enumerate(links)])


# ---- requests

def record_request(db, person: str, link: str, joined: bool = False, at: str = "") -> None:
    db.execute("INSERT INTO requests (at, person, link, joined) VALUES (?, ?, ?, ?)", (at or utc_now(), person, link, int(joined)))


def requests(db) -> list[tuple[str, str, str, bool]]:
    return [(row["at"], row["person"], row["link"], bool(row["joined"]))
            for row in db.execute("SELECT at, person, link, joined FROM requests ORDER BY id")]


# ---- outbox

def send_later(db, person: str, text: str) -> None:
    db.execute("INSERT INTO outbox (created, person, text, next_try) VALUES (?, ?, ?, ?)", (utc_now(), person, text, utc_now()))


def meta(db, key: str, value: str | None = None) -> str:
    if value is not None:
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        return value
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""
