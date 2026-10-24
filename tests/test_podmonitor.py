#!/usr/bin/env python3
"""Unit tests for the subscription monitor: first sight, new episodes, Apple lag, trailers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.parse
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import podfetch
import podmonitor
import podqueue
import podshows

SHOW = "https://podcasts.apple.com/gb/podcast/ask-the-astronomer/id1000000101"


def episode(guid: str, title: str = "", duration: str = "3600") -> podfetch.Episode:
    return podfetch.Episode(guid=guid, title=title or guid, duration=duration)


class MonitorTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.state = root / "state"
        (root / "notes" / "Podcasts").mkdir(parents=True)
        for module, name, value in ((podmonitor, "STATE", self.state), (podshows, "STATE", self.state),
                                    (podqueue, "STATE", self.state), (podqueue, "ROOT", root / "notes"),
                                    (podqueue, "EPISODES", Path("Podcasts"))):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = podqueue.parse_iso("2026-10-08T10:00:00Z")
        patcher = mock.patch.object(podqueue, "now", lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state.mkdir()
        (self.state / "shows.json").write_text(json.dumps({      # one monitored show, one only processed
            "1000000101": {"title": "Ask the Astronomer", "folder": "Ask the Astronomer", "country": "gb",
                           "monitor": True, "hosts": [], "fixes": {}, "advertisers": {}},
            "1000000102": {"title": "Make-Do", "folder": "Make-Do", "country": "us", "monitor": False,
                           "hosts": [], "fixes": {}, "advertisers": {}}}), encoding="utf-8")
        self.feed = [episode("old-1"), episode("old-2")]
        self.apple = {}

    def check(self) -> str:
        with mock.patch.object(podmonitor, "feed_episodes", lambda show, country: list(self.feed)), \
                mock.patch.object(podmonitor, "apple_links", lambda show, country: dict(self.apple)):
            return podmonitor.check()

    def queue(self) -> list[str]:
        return podqueue.read_lines(self.state / "queue.txt")

    def test_only_monitored_shows_are_checked(self):
        self.assertEqual([("1000000101", "gb")], podmonitor.subscriptions())

    def test_first_sight_marks_the_back_catalogue_seen(self):
        self.assertEqual("", self.check())
        self.assertEqual([], self.queue())
        state = json.loads((self.state / "monitor-state.json").read_text())["1000000101"]
        self.assertEqual(["old-1", "old-2"], state["seen"])

    def test_new_episode_is_queued_with_its_apple_link_oldest_first(self):
        self.check()
        self.feed = [episode("new-2"), episode("new-1")] + self.feed     # feeds list newest first
        self.apple = {"new-1": "https://podcasts.apple.com/gb/podcast/id1000000101?i=1000000000001",
                      "new-2": "https://podcasts.apple.com/gb/podcast/id1000000101?i=1000000000002"}
        self.assertEqual("", self.check())
        self.assertEqual(["https://podcasts.apple.com/gb/podcast/id1000000101?i=1000000000001",
                          "https://podcasts.apple.com/gb/podcast/id1000000101?i=1000000000002"], self.queue())
        self.assertEqual("", self.check())                                # already seen: nothing new
        self.assertEqual(2, len(self.queue()))

    def test_trailers_are_skipped(self):
        self.check()
        self.feed = [episode("teaser", duration="00:02:30")] + self.feed
        self.apple = {"teaser": "https://podcasts.apple.com/gb/podcast/id1000000101?i=1000000000009"}
        self.check()
        self.assertEqual([], self.queue())

    def test_episode_not_yet_on_apple_waits_then_gives_up_after_three_days(self):
        self.check()
        self.feed = [episode("late", "Episode 152")] + self.feed
        self.assertEqual("", self.check())
        self.clock += timedelta(days=1)
        self.apple = {}
        self.assertEqual("", self.check())
        self.clock += timedelta(days=2)
        self.assertEqual("Podcast not on Apple after 3 days, skipped: Episode 152", self.check())
        self.assertEqual("", self.check())

    def test_episode_appearing_on_apple_later_is_queued(self):
        self.check()
        self.feed = [episode("late")] + self.feed
        self.check()
        self.apple = {"late": "https://podcasts.apple.com/gb/podcast/id1000000101?i=1000000000003"}
        self.check()
        self.assertEqual(["https://podcasts.apple.com/gb/podcast/id1000000101?i=1000000000003"], self.queue())

    def test_monitor_uses_direct_resolvers_window_for_new_apple_episode(self):
        self.check()  # initialise with the existing catalogue
        self.feed = [episode("new-guid", "Just published")] + self.feed
        queries = []

        def apple_response(url):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            queries.append(params)
            # Apple's smaller lookup window may lag its larger response.
            rows = ([{"episodeGuid": "new-guid", "trackId": 1000000000004}]
                    if params["limit"] == [str(podfetch.APPLE_EPISODE_LIMIT)] else [])
            return 200, json.dumps({"results": rows}).encode(), {}

        with mock.patch.object(podmonitor, "feed_episodes", lambda show, country: list(self.feed)), \
                mock.patch.object(podfetch, "http_get", side_effect=apple_response):
            self.assertEqual("", podmonitor.check())

        self.assertEqual(["https://podcasts.apple.com/gb/podcast/id1000000101?i=1000000000004"], self.queue())
        self.assertTrue(queries)
        self.assertTrue(all(query["limit"] == [str(podfetch.APPLE_EPISODE_LIMIT)] for query in queries))
        state = json.loads((self.state / "monitor-state.json").read_text())["1000000101"]
        self.assertIn("new-guid", state["seen"])
        self.assertNotIn("new-guid", state["pending"])

    def test_feed_failure_is_silent_and_recorded(self):
        def broken(show, country):
            raise LookupError("feed returned HTTP 503")
        with mock.patch.object(podmonitor, "feed_episodes", broken):
            self.assertEqual("", podmonitor.check())
        state = json.loads((self.state / "monitor-state.json").read_text())["1000000101"]
        self.assertIn("HTTP 503", state["last_error"])
        self.assertNotIn("initialised", state)


if __name__ == "__main__":
    unittest.main()
