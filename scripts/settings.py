#!/usr/bin/env python3
"""Load the project's .env into the environment.

Every PODNOTE_* and OMLX_* default is read from os.environ, so this must run before any
module computes one. Importing it has that effect; it never overrides a variable that is
already set, so the shell still wins over the file.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(os.environ.get("PODNOTE_ENV_FILE") or Path(__file__).resolve().parents[1] / ".env")


def load(path: Path = ENV_FILE) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


load()
