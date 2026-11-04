#!/usr/bin/env python3
"""The settings loader reads .env.example as documented."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import settings

EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


class SettingsTests(unittest.TestCase):

    def test_the_example_reads_as_documented(self):
        found = settings.parse(EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual("/path/to/your/notes", found["PODNOTE_ROOT"])           # its inline comment left out
        self.assertEqual("/var/tmp/podnote-state", found["PODNOTE_STATE"])
        for empty in ("PODNOTE_YTDLP", "PODNOTE_BRAVE_API_KEY", "PODNOTE_CONTACT", "PODNOTE_SCRIPT_TAG", "OMLX_API_KEY"):
            self.assertEqual("", found[empty], empty)
        self.assertNotIn("PODNOTE_BASE_URL", found)                              # commented out

    def test_quotes_and_hashes(self):
        found = settings.parse('A="x # y"\nB=abc#def\nC=\'q\' # note\nD=url # note\nE=   # empty\n')
        self.assertEqual({"A": "x # y", "B": "abc#def", "C": "q", "D": "url", "E": ""}, found)


if __name__ == "__main__":
    unittest.main()
