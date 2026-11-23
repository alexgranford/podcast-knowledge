#!/usr/bin/env python3
"""The model server key: only OMLX_SCRIPT_API_KEY is read (6.2), never the old OMLX_API_KEY, and no secret is ever printed."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import omlx_client  # noqa: E402

BASE = {"OMLX_BASE_URL": "http://127.0.0.1:8000"}


class KeyTests(unittest.TestCase):
    def config(self, env):
        with patch.dict(os.environ, {**BASE, **env}, clear=True), patch("settings.load", lambda *a, **k: None):
            return omlx_client.config()

    def test_the_script_key_is_used_when_both_names_are_set(self):
        self.assertEqual("script-dummy", self.config({"OMLX_SCRIPT_API_KEY": "script-dummy", "OMLX_API_KEY": "hermes-dummy"})["api_key"])

    def test_the_old_name_alone_is_not_enough_and_no_secret_is_shown(self):
        with self.assertRaises(omlx_client.OMLXError) as caught:
            self.config({"OMLX_API_KEY": "hermes-dummy-secret"})
        self.assertIn("OMLX_SCRIPT_API_KEY is not configured", str(caught.exception))
        self.assertNotIn("hermes-dummy-secret", str(caught.exception))

    def test_an_empty_script_key_is_missing(self):
        with self.assertRaises(omlx_client.OMLXError):
            self.config({"OMLX_SCRIPT_API_KEY": ""})


if __name__ == "__main__":
    unittest.main()
