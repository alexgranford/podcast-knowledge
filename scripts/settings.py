#!/usr/bin/env python3
"""Load the project's .env into the environment.

Every PODNOTE_* and OMLX_* default is read from os.environ, so this must run before any
module computes one. Importing it has that effect; it never overrides a variable that is
already set, so the shell still wins over the file.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ENV_FILE = Path(os.environ.get("PODNOTE_ENV_FILE") or Path(__file__).resolve().parents[1] / ".env")


def parse(text: str) -> dict[str, str]:
    """NAME=value lines as a dict: blank lines and # lines skipped; a quoted value is taken as quoted;
    an unquoted one ends at a # after a space (an inline comment), as in .env.example."""
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in ("'", '"') and value[0] in value[1:]:
            value = value[1:value.index(value[0], 1)]
        else:
            value = "" if value.startswith("#") else re.split(r"\s+#", value, maxsplit=1)[0].strip()
        found[name.strip()] = value
    return found


def load(path: Path = ENV_FILE) -> None:
    if not path.is_file():
        return
    for name, value in parse(path.read_text(encoding="utf-8", errors="replace")).items():
        os.environ.setdefault(name, value)


load()
