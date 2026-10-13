#!/usr/bin/env python3
"""Unit tests for the podnote queue: adding links, running the next one, retries and verdicts."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import podqueue

EP1 = "https://podcasts.apple.com/gb/podcast/show/id123?i=1000000000001"
EP2 = "https://podcasts.apple.com/gb/podcast/show/id123?i=1000000000002"


class QueueTestCase(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / "state"
        self.notes = Path(directory.name) / "notes"
        (self.notes / "Episodes").mkdir(parents=True)
        for name, value in (("STATE", self.state), ("ROOT", self.notes), ("EPISODES", Path("Episodes"))):
            patcher = mock.patch.object(podqueue, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = podqueue.parse_iso("2026-10-08T10:00:00Z")
        patcher = mock.patch.object(podqueue, "now", lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def queue(self) -> list[str]:
        return podqueue.read_lines(self.state / "queue.txt")

    def run_with(self, code: int, output: str) -> str:
        with mock.patch.object(podqueue, "run_podnote", return_value=(code, output)) as runner, \
                mock.patch.object(podqueue, "run_verify", return_value="verified: 2 fixes, 3 claims checked") as qa:
            message = podqueue.run()
        self.qa_ran = qa.call_args.args[0] if qa.called else None
        self.ran = runner.call_args.args[0] if runner.called else None
        return message


class AddTests(QueueTestCase):

    def test_episode_link_is_queued_in_order(self):
        self.assertEqual("Queued (position 1 of 1).", podqueue.add(EP1, "telegram"))
        self.assertEqual("Queued (position 2 of 2).", podqueue.add(f"  <{EP2}>  "))
        self.assertEqual([EP1, EP2], self.queue())
        self.assertEqual("telegram", json.loads((self.state / "queue-state.json").read_text())[EP1]["source"])

    def test_duplicates_show_links_and_foreign_links_are_refused(self):
        podqueue.add(EP1)
        self.assertIn("Already queued", podqueue.add(EP1.replace("/gb/", "/us/")))
        self.assertIn("show link", podqueue.add("https://podcasts.apple.com/gb/podcast/show/id123"))
        self.assertIn("Not queued", podqueue.add("https://example.com/podcast/x/id1?i=2"))
        self.assertEqual([EP1], self.queue())

    def test_episode_already_in_the_library_is_not_queued(self):
        (self.notes / "Episodes" / "note.md").write_text("---\napple_episode_id: '1000000000001'\n---\n")
        self.assertIn("Already in the library", podqueue.add(EP1))
        self.assertEqual([], self.queue())


class RunTests(QueueTestCase):

    def test_verify_status_is_parsed_from_podverify_output(self):
        with mock.patch.object(podqueue, "run_owned", return_value=(0, "Verified: 1 changes, 4 claims\n")):
            self.assertEqual("verified: 1 fix, 4 claims checked", podqueue.run_verify("/n.md"))
        with mock.patch.object(podqueue, "run_owned", return_value=(1, "STOPPED: no verification model: busy")):
            self.assertEqual("verification failed: STOPPED: no verification model: busy", podqueue.run_verify("/n.md"))
        with mock.patch.object(podqueue, "run_owned", return_value=None):
            self.assertEqual("verification timed out", podqueue.run_verify("/n.md"))

    def test_a_timed_out_run_is_stopped_with_its_children(self):
        import sys
        child = [sys.executable, "-c", "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', "
                 "'import time; time.sleep(30)']); time.sleep(30)"]
        self.assertIsNone(podqueue.run_owned(child, 1))                        # the whole group is gone after a timeout
        self.assertEqual((0, "ok\n"), podqueue.run_owned([sys.executable, "-c", "print('ok')"], 30))

    def test_empty_queue_is_silent_and_runs_nothing(self):
        self.assertEqual("", self.run_with(0, ""))
        self.assertIsNone(self.ran)

    def test_complete_note_is_reported_and_dequeued(self):
        podqueue.add(EP1)
        podqueue.add(EP2)
        note = self.state / "notes" / "The Daily Courier" / "2026-10-07 - Forecasts.md"
        note.parent.mkdir(parents=True)
        note.write_text('---\nepisode: "Forecasts: Who is right?"\n---\n# Forecasts\n\n> [!abstract] Summary\n> **Participants:** '
                        "**Clara Benton** (host), **Hugo Marsh** (host)\n>\n> Why Labour trails in the polls.\n>\n"
                        "> A longer paragraph on the arguments.\n\n## Key themes\n", encoding="utf-8")
        message = self.run_with(0, f"NOTE: {note}\nSTATUS: complete\n")
        self.assertEqual(EP1, self.ran)
        self.assertEqual("🟢 The Daily Courier: Forecasts: Who is right?\nClara Benton (host), Hugo Marsh (host)\n"
                         "Why Labour trails in the polls.", message)
        self.assertEqual(str(note), self.qa_ran)
        self.assertEqual([EP2], self.queue())
        self.assertIn("complete\t2026-10-07 - Forecasts", (self.state / "done.txt").read_text())

    def test_an_unverified_note_is_reported_like_any_other(self):
        podqueue.add(EP1)
        with mock.patch.object(podqueue, "run_podnote", return_value=(0, "NOTE: /v/Show/n.md\nSTATUS: complete\n")), \
                mock.patch.object(podqueue, "run_verify", return_value="verification failed: STOPPED: no verification model"):
            message = podqueue.run()
        self.assertTrue(message.startswith("🟢 "), message)
        self.assertNotIn("verif", message.lower())                         # the message never mentions it

    def test_existing_note_is_dequeued_silently(self):
        podqueue.add(EP1)
        self.assertEqual("", self.run_with(0, "note already exists (use --force to overwrite): /v/x.md\n"))
        self.assertIsNone(self.qa_ran)
        self.assertEqual([], self.queue())

    def test_another_run_holding_the_lock_is_not_a_failure(self):
        podqueue.add(EP1)
        self.assertEqual("", self.run_with(1, "STOPPED: another podnote run is active\n"))
        self.assertEqual([EP1], self.queue())
        self.assertNotIn("next_try", json.loads((self.state / "queue-state.json").read_text())[EP1])
        # the speech model busy with another run's transcription: wait, no retry delay either
        self.assertEqual("", self.run_with(1, "STOPPED: Shared ASR slot is already active\n"))
        self.assertNotIn("next_try", json.loads((self.state / "queue-state.json").read_text())[EP1])

    def test_hard_failure_is_reported_at_once(self):
        podqueue.add(EP1)
        message = self.run_with(1, "STOPPED: ffprobe rejected audio: invalid data\n")
        self.assertEqual(f"🔴 Podcast: {EP1}\nThe audio file could not be read.", message)
        self.assertEqual([], self.queue())
        self.assertIn("ffprobe rejected audio", (self.state / "failed.txt").read_text())

    def test_not_indexed_retries_at_1_3_6_12_hours_then_fails(self):
        podqueue.add(EP1)
        not_indexed = "STOPPED: Episode not found. Apple has not indexed this episode yet — try again later.\n"
        start = self.clock
        for hours in (1, 3, 6, 12):
            self.assertEqual("", self.run_with(1, not_indexed))
            entry = json.loads((self.state / "queue-state.json").read_text())[EP1]
            self.assertEqual(podqueue.iso(start + timedelta(hours=hours)), entry["next_try"])
            self.assertEqual("", self.run_with(1, not_indexed))          # not due yet: nothing runs
            self.assertIsNone(self.ran)
            self.clock = start + timedelta(hours=hours)
        message = self.run_with(1, not_indexed)
        self.assertTrue(message.startswith(f"🔴 Podcast: {EP1}\n"), message)
        self.assertTrue(message.endswith("(5 tries)"), message)
        self.assertEqual([], self.queue())

    def test_a_waiting_retry_does_not_block_the_next_link(self):
        podqueue.add(EP1)
        podqueue.add(EP2)
        self.run_with(1, "STOPPED: model server unreachable at section 1: refused\n")
        self.run_with(0, "NOTE: /v/E/two.md\nSTATUS: complete\n")
        self.assertEqual(EP2, self.ran)
        self.assertEqual([EP1], self.queue())

    def test_listing_shows_pending_retries(self):
        podqueue.add(EP1)
        self.run_with(1, "STOPPED: model server unreachable at section 1: refused\n")
        self.assertIn("retry 1 due 2026-10-08T11:00:00Z", podqueue.listing())


if __name__ == "__main__":
    unittest.main()


class QuickSummaryTests(unittest.TestCase):

    def test_an_older_note_gives_its_paragraph_first_sentence_not_its_tone_line(self):
        old = ("> [!abstract] Summary\n> **Participants:** **A** (host)\n>\n> **Sentiment:** Analytical and professional.\n"
               ">\n> Reinsurers face a softening market. Capital keeps arriving.\n")
        self.assertEqual("Reinsurers face a softening market.", podqueue.quick_summary(old))
        self.assertEqual("", podqueue.quick_summary("no callout here"))


class PlainReasonTests(unittest.TestCase):

    def test_failures_are_said_plainly(self):
        say = podqueue.plain_reason
        self.assertEqual("The audio could not be split for transcription (no pause to cut at).",
                         say("vad_boundary_unavailable: window_start_ms=0 target_ms=900000 corridor_ms=780000:1020000"))
        self.assertEqual("Apple has not listed this episode yet.", say("Apple episode 1 was not returned for show 2; ..."))
        self.assertEqual("The model ran out of room writing section 3 (0:24:17).",
                         say("note incomplete: section 3 (0:24:17): length"))
        self.assertEqual("Hermes is missing the Python package jsonschema.", say("ModuleNotFoundError: No module named 'jsonschema'"))
        self.assertEqual("Something new went wrong", say("Traceback\nSomething new went wrong"))
