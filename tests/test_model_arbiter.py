#!/usr/bin/env python3
"""Focused model-residency contract test."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import model_arbiter


class ModelResidencyTests(unittest.TestCase):
    def test_local_text_session_finishes_with_no_loaded_model(self) -> None:
        loaded = [model_arbiter.omlx_text.ASR_MODEL]

        def loaded_models(_config):
            return list(loaded)

        def transition(_config, model, load, timeout=1200):
            del timeout
            if load and model not in loaded:
                loaded.append(model)
            if not load and model in loaded:
                loaded.remove(model)
            return {"id": model, "loaded": load}

        invented = {"OMLX_BASE_URL": "http://127.0.0.1:8000", "OMLX_API_KEY": "test-key"}   # no server is called
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, invented), \
             patch.object(model_arbiter.omlx_text, "loaded_models", loaded_models), \
             patch.object(model_arbiter.omlx_text, "transition", transition):
            with model_arbiter.local_model_session(
                "podcast-test", "test", state_root=directory,
                model="Example-27B-4bit",
            ):
                self.assertEqual(["Example-27B-4bit"], loaded)
            self.assertEqual([], loaded)
            self.assertFalse(model_arbiter.lease_path(directory).exists())
            loaded.append("Example-9B-8bit")
            model_arbiter.lease_path(directory).write_text('{"stale":true}\n', encoding="utf-8")
            reconciled = model_arbiter.reconcile(directory)
            self.assertTrue(reconciled["cleaned"])
            self.assertEqual([], reconciled["loaded_models"])
            self.assertIsNone(reconciled["lease"])


if __name__ == "__main__":
    unittest.main()
