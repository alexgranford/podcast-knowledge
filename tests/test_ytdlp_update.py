"""ytdlp_update: an upgrade that fails the YouTube test is rolled back."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import ytdlp_update


class UpdateTests(unittest.TestCase):

    def run_update(self, versions, results):
        installs = []
        with mock.patch.dict("os.environ", {"PODNOTE_YTDLP": __file__}), \
                mock.patch.object(ytdlp_update, "pip", lambda program, *a: installs.append(a[-1]) or mock.Mock(returncode=0)), \
                mock.patch.object(ytdlp_update, "version", side_effect=versions), \
                mock.patch.object(ytdlp_update, "smoke_test", side_effect=results), \
                mock.patch("builtins.print") as said:
            code = ytdlp_update.main()
        return code, installs, " ".join(str(c.args[0]) for c in said.call_args_list)

    def test_a_passing_upgrade_is_silent(self):
        self.assertEqual((0, ["yt-dlp[default]"], ""), self.run_update(["2026.10.26", "2026.11.02"], [""]))

    def test_a_failing_upgrade_is_rolled_back(self):
        code, installs, said = self.run_update(["2026.10.26", "2026.11.02"], ["bot check", ""])
        self.assertEqual((1, ["yt-dlp[default]", "yt-dlp[default]==2026.10.26"]), (code, installs))
        self.assertIn("put 2026.10.26 back, which passes it", said)

    def test_no_newer_release_is_reported_not_rolled_back(self):
        code, installs, said = self.run_update(["2026.10.26", "2026.10.26"], ["nsig"])
        self.assertEqual((1, ["yt-dlp[default]"]), (code, installs))
        self.assertIn("no newer release yet", said)
