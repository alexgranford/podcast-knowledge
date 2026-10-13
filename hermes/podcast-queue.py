#!/usr/bin/env python3
"""Hermes cron wrapper (script mode, no model): one tick of the podcast-knowledge queue: the next episode, if any.

Copy to ~/.hermes/scripts/ (in Docker /opt/data/scripts/): Hermes only runs cron scripts from there.
It finds the skill under ~/.hermes/skills/ (or PODNOTE_SKILL_DIR) and runs it on the Python that has
the skill's requirements: PODNOTE_PYTHON, else the Hermes Docker image's /opt/hermes/.venv, else this one.
Hermes may start cron scripts on its own managed environment, which lacks those packages."""
import os
import sys
from pathlib import Path

SCRIPT, ARGS = "podqueue.py", ["run"]

home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
skill = os.environ.get("PODNOTE_SKILL_DIR")
found = [Path(skill)] if skill else sorted(home.glob("skills/**/podcast-knowledge"))
if not found or not (found[0] / "scripts" / SCRIPT).is_file():
    sys.exit(f"podcast-knowledge skill not found under {home / 'skills'}; set PODNOTE_SKILL_DIR")
script = found[0] / "scripts" / SCRIPT
docker_venv = "/opt/hermes/.venv/bin/python3"
python = os.environ.get("PODNOTE_PYTHON") or (docker_venv if os.path.exists(docker_venv) else sys.executable)

if os.path.realpath(sys.prefix) != os.path.realpath(Path(python).parent.parent):
    os.execv(python, [python, str(script), *ARGS])      # same process, the right interpreter

import runpy  # noqa: E402

sys.argv = [script.name, *ARGS]
runpy.run_path(str(script), run_name="__main__")
