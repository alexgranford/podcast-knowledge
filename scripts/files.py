"""files — writing state, records and notes so that a write lands whole or not at all. The one atomic
writer: text, bytes and JSON go through atomic_bytes."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_bytes(path: str | Path, body: bytes, mode: int | None = None) -> None:
    """Write body to path through a temporary file in the same folder, flushed to disk, then renamed
    over the old one: a reader never sees half a file. mode: the new file's permissions (e.g. 0o600)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def atomic_write(path: str | Path, text: str, mode: int | None = None) -> None:
    """Text, as UTF-8 (see atomic_bytes)."""
    atomic_bytes(path, text.encode("utf-8"), mode)


def atomic_json(path: str | Path, value) -> None:
    """A JSON value, indented, non-ASCII kept, with a final newline (see atomic_bytes)."""
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
