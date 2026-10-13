#!/usr/bin/env python3
"""Acceptance tests for the isolated podcast VAD runtime."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "podcast_vad.py"
MODEL = ROOT / "assets" / "silero-vad" / "6.2.1" / "model.onnx"
POLICY = ROOT / "references" / "current" / "asr-window-policy-v1.json"
MANIFEST = ROOT / "references" / "current" / "vad-model-manifest.json"
RUNTIME = Path(os.environ.get("PODNOTE_VAD_RUNTIME", "/var/lib/podnote/vad-runtime"))
PYTHON = RUNTIME / "venv" / "bin" / "python"
READY = RUNTIME / "READY.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@unittest.skipUnless(READY.is_file(), "needs the speech-detection runtime (PODNOTE_VAD_RUNTIME)")
class RuntimeTests(unittest.TestCase):
    def test_manifest_and_ready_hashes_match_installed_assets(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        ready = json.loads(READY.read_text(encoding="utf-8"))
        self.assertEqual("ready", ready["status"])
        self.assertEqual(manifest["model_sha256"], sha256(MODEL))
        self.assertEqual(ready["model_sha256"], sha256(MODEL))
        self.assertEqual(ready["cli_sha256"], sha256(CLI))
        self.assertEqual(ready["policy_sha256"], sha256(POLICY))
        self.assertEqual(ready["requirements_lock_sha256"], sha256(RUNTIME / "requirements.lock"))

    def test_runtime_self_test_is_deterministic_and_cpu_only(self) -> None:
        result = subprocess.run(
            [str(PYTHON), str(CLI), "self-test", "--model", str(MODEL)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        body = json.loads(result.stdout)
        self.assertTrue(body["passed"])
        self.assertEqual(["CPUExecutionProvider"], body["provider"])
        self.assertEqual(0.0, body["repeat_max_delta"])
        self.assertEqual("", result.stderr)



if __name__ == "__main__":
    unittest.main(verbosity=2)
