#!/usr/bin/env python3
"""setup_vad — build the speech-detection (VAD) runtime that transcription checks before every job.

    python3 scripts/setup_vad.py [--runtime DIR] [--python PYTHON]

Creates DIR/venv with Silero VAD's runtime (ONNX Runtime and NumPy, from requirements-vad.txt), records
the installed versions in DIR/requirements.lock, runs the VAD self-test on the bundled model and writes
DIR/READY.json with the checksums transcription compares before it runs: the model,
scripts/podcast_vad.py, the window policy and the lock. DIR defaults to PODNOTE_VAD_RUNTIME.

Run it again whenever transcription stops with "VAD runtime readiness/hash validation failed", which
means one of those files changed (usually after updating the skill). It is safe to re-run.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402,F401  (importing it loads the project .env)
from podcast_state import file_sha256  # noqa: E402
from podcast_transcription import DEFAULT_RUNTIME, DEFAULT_VAD_CLI, DEFAULT_VAD_MODEL, DEFAULT_WINDOW_POLICY  # noqa: E402

REQUIREMENTS = HERE.parent / "requirements-vad.txt"


def run(*command) -> str:
    return subprocess.run([str(part) for part in command], check=True, capture_output=True, text=True).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runtime", default=str(DEFAULT_RUNTIME), help="runtime folder (default: PODNOTE_VAD_RUNTIME)")
    parser.add_argument("--python", default=sys.executable, help="Python used to create the venv (3.11+)")
    args = parser.parse_args()
    runtime = Path(args.runtime).expanduser()
    python = runtime / "venv" / "bin" / "python"
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        if not python.is_file():
            print(f"creating {runtime / 'venv'}")
            run(args.python, "-m", "venv", runtime / "venv")
        print(f"installing {REQUIREMENTS.name}")
        run(python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", REQUIREMENTS)
        lock = runtime / "requirements.lock"
        lock.write_text(run(python, "-m", "pip", "freeze", "--disable-pip-version-check"), encoding="utf-8")
        test = json.loads(run(python, DEFAULT_VAD_CLI, "self-test", "--model", DEFAULT_VAD_MODEL))
    except subprocess.CalledProcessError as error:
        print(f"FAILED: {' '.join(map(str, error.cmd))}\n{(error.stderr or error.stdout or '').strip()[-2000:]}")
        return 1
    except OSError as error:
        print(f"FAILED: {error}")
        return 1
    ready = {
        "schema_version": 1,
        "status": "ready",
        "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interpreter": str(python),
        "requirements_lock_sha256": file_sha256(lock),
        "model_sha256": file_sha256(DEFAULT_VAD_MODEL),
        "cli_sha256": file_sha256(DEFAULT_VAD_CLI),
        "policy_sha256": file_sha256(DEFAULT_WINDOW_POLICY),
        "self_test": {key: test[key] for key in ("passed", "provider", "silence_max_probability", "repeat_max_delta")},
    }
    temporary = runtime / "READY.json.tmp"
    temporary.write_text(json.dumps(ready, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, runtime / "READY.json")
    print(f"VAD runtime ready: {runtime} (onnxruntime {test['onnxruntime']}, numpy {test['numpy']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
