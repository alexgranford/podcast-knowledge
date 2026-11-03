#!/usr/bin/env python3
"""Unit tests for the subscription monitor: first sight, new episodes, Apple lag, trailers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import podfetch
import podmonitor
import podqueue
import podshows

SHOW = "https://podcasts.apple.com/gb/podcast/ask-the-astronomer/id1000000101"


def episode(guid: str, title: str = "", duration: str = "3600", enclosure: str = "",
            published: datetime | None = None) -> podfetch.Episode:
    return podfetch.Episode(guid=guid, title=title or guid, duration=duration,
                            enclosure_url=enclosure, published=published)


class MonitorFixture(unittest.TestCase):
    """One monitored show, one only processed, a state folder and a clock; no tests of its own."""

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
                           "followers": ["admin"], "hosts": [], "fixes": {}, "advertisers": {}},
            "1000000102": {"title": "Make-Do", "folder": "Make-Do", "country": "us", "followers": [],
                           "hosts": [], "fixes": {}, "advertisers": {}}}), encoding="utf-8")
        self.feed = [episode("old-1"), episode("old-2")]
        self.apple: dict[str, str] = {}

    def check(self) -> str:
        hints = [podfetch.AppleEpisodeHint(track_id=url.rsplit("=", 1)[-1], episode_guid=guid)
                 for guid, url in self.apple.items()]
        with mock.patch.object(podmonitor, "feed_episodes", lambda show, country: list(self.feed)), \
                mock.patch.object(podmonitor, "apple_episodes", lambda show, country: hints):
            return podmonitor.check()

    def queue(self) -> list[str]:
        return podqueue.read_lines(self.state / "queue.txt")


class MonitorTests(MonitorFixture):

    def test_only_monitored_shows_are_checked(self):
        self.assertEqual([("1000000101", "gb")], podshows.monitored())

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

    def test_unresolved_notifies_once_after_three_days_but_recovers_later(self):
        self.check()
        self.feed = [episode("late", "Episode 152")] + self.feed
        self.assertEqual("", self.check())
        self.clock += timedelta(days=1)
        self.apple = {}
        self.assertEqual("", self.check())
        self.clock += timedelta(days=2)
        self.assertEqual("Podcast still unresolved on Apple after 3 days (will retry): Episode 152", self.check())
        self.assertEqual("", self.check())
        entry = json.loads((self.state / "monitor-state.json").read_text())["1000000101"]
        self.assertIn("late", entry["pending"])
        self.assertNotIn("late", entry["seen"])
        self.apple = {"late": "https://podcasts.apple.com/gb/podcast/id1000000101?i=1000000000003"}
        self.assertEqual("", self.check())
        self.assertEqual(1, len(self.queue()))
        self.assertNotIn("late", json.loads((self.state / "monitor-state.json").read_text())["1000000101"]["pending"])

    def test_episode_appearing_on_apple_later_is_queued(self):
        self.check()
        self.feed = [episode("late")] + self.feed
        self.check()
        self.apple = {"late": "https://podcasts.apple.com/gb/podcast/id1000000101?i=1000000000003"}
        self.check()
        self.assertEqual(["https://podcasts.apple.com/gb/podcast/id1000000101?i=1000000000003"], self.queue())

    def test_a_pass_already_running_is_left_alone(self):
        import fcntl
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / "monitor-run.lock").open("a+") as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(podmonitor, "check_apple") as apple, mock.patch.object(podmonitor, "check_youtube") as yt:
                self.assertEqual("", podmonitor.check())
        apple.assert_not_called(); yt.assert_not_called()
        self.assertFalse((self.state / "monitor-state.json").exists())

    def test_a_fault_on_one_show_is_reported_once_and_the_pass_goes_on(self):
        with podshows.editing() as registry:
            registry["1000000102"]["followers"] = ["admin"]
        def flaky(show, state, messages=None):                                # a bug, not a source failure
            if show == "1000000101":
                raise KeyError("broken")
        with mock.patch.object(podmonitor, "feed_episodes", lambda show, country: list(self.feed)), \
                mock.patch.object(podmonitor, "apple_episodes", lambda s, c: []), \
                mock.patch.object(podmonitor, "check_youtube", flaky):
            first, second = podmonitor.check(), podmonitor.check()
        self.assertEqual("⚠️ Ask the Astronomer: monitor error: KeyError: 'broken'", first)
        self.assertEqual("", second)                                           # once, not every pass
        state = json.loads((self.state / "monitor-state.json").read_text())
        self.assertIn("initialised", state["1000000102"])                      # the next show was checked and saved

    def test_a_source_unread_for_two_days_is_reported_once_then_cleared(self):
        self.check()
        def down(show, country):
            raise LookupError("feed returned HTTP 503")
        with mock.patch.object(podmonitor, "feed_episodes", down):
            self.assertEqual("", podmonitor.check())                           # a bad day: silent
            self.clock += podqueue.timedelta(hours=49)
            self.assertIn("Ask the Astronomer: not read since 2026-10-08 10:00 UTC", podmonitor.check())
            self.assertEqual("", podmonitor.check())
        self.assertEqual("", self.check())                                     # read again: healthy
        state = json.loads((self.state / "monitor-state.json").read_text())["1000000101"]
        self.assertNotIn("failing_since", state)
        self.assertEqual("2026-10-10T11:00:00Z", state["last_success"])

    def test_a_copy_held_in_the_old_form_keeps_its_wait(self):
        entry = {"held": {"g1": "2026-10-08T09:00:00Z"}, "seen": [], "pending": {}}
        watch = podmonitor.Watch(entry, podmonitor.BOUNDED)
        self.assertEqual({"since": "2026-10-08T09:00:00Z", "peek": {"title": "t"}}, watch.hold("g1", {"title": "t"}))

    def test_a_held_apple_episode_the_feed_rolled_off_is_still_made(self):
        self.check()                                                          # first sight
        link = "https://podcasts.apple.com/gb/podcast/id1000000101?i=1000000000009"
        state = json.loads((self.state / "monitor-state.json").read_text())
        state["1000000101"]["held"] = {"gone": {"since": "2026-10-07T00:00:00Z", "link": link, "peek": {
            "show": "1000000101", "title": "Gone", "published": "2026-10-07T00:00:00", "seconds": 3600, "source": "apple"}}}
        (self.state / "monitor-state.json").write_text(json.dumps(state))
        self.check()                                                          # not in the feed any more
        self.assertEqual([link], self.queue())

    def test_feed_failure_is_silent_and_recorded(self):
        def broken(show, country):
            raise LookupError("feed returned HTTP 503")
        with mock.patch.object(podmonitor, "feed_episodes", broken):
            self.assertEqual("", podmonitor.check())
        state = json.loads((self.state / "monitor-state.json").read_text())["1000000101"]
        self.assertIn("HTTP 503", state["last_error"])
        self.assertNotIn("initialised", state)

    def test_different_guid_same_enclosure_queues_once(self):
        self.check()
        self.feed = [episode("rss-guid", "Episode", enclosure="https://example.com/a.mp3")] + self.feed
        hint = podfetch.AppleEpisodeHint(track_id="1000000000005", episode_guid="apple-guid",
                                         episode_url="https://example.com/a.mp3", title="Episode")
        with mock.patch.object(podmonitor, "feed_episodes", lambda *_: list(self.feed)), \
                mock.patch.object(podmonitor, "apple_episodes", lambda *_: [hint]):
            self.assertEqual("", podmonitor.check())
            self.assertEqual("", podmonitor.check())
        self.assertEqual(1, len(self.queue()))

    def test_title_only_does_not_queue_without_date_and_duration(self):
        self.check()
        self.feed = [episode("rss-guid", "Episode")] + self.feed
        hint = podfetch.AppleEpisodeHint(track_id="1000000000005", title="Episode")
        with mock.patch.object(podmonitor, "feed_episodes", lambda *_: list(self.feed)), \
                mock.patch.object(podmonitor, "apple_episodes", lambda *_: [hint]):
            self.assertEqual("", podmonitor.check())
        self.assertFalse(self.queue())
        self.assertIn("rss-guid", json.loads((self.state / "monitor-state.json").read_text())["1000000101"]["pending"])

    def test_title_date_duration_matching_requires_close_metadata(self):
        self.check()
        published = datetime(2026, 10, 9, tzinfo=timezone.utc)
        self.feed = [episode("rss-guid", "Episode", "3600", published=published)] + self.feed
        hint = podfetch.AppleEpisodeHint(track_id="1000000000005", title="Episode",
                                         published=published, duration_seconds=3600)
        with mock.patch.object(podmonitor, "feed_episodes", lambda *_: list(self.feed)), \
                mock.patch.object(podmonitor, "apple_episodes", lambda *_: [hint]):
            self.assertEqual("", podmonitor.check())
        self.assertEqual(1, len(self.queue()))

    def test_ambiguous_duplicate_apple_tracks_do_not_queue(self):
        self.check()
        self.feed = [episode("rss-guid", "Episode", enclosure="https://example.com/a.mp3")] + self.feed
        hints = [podfetch.AppleEpisodeHint(track_id=str(id), episode_url="https://example.com/a.mp3")
                 for id in (1000000000005, 1000000000006)]
        with mock.patch.object(podmonitor, "feed_episodes", lambda *_: list(self.feed)), \
                mock.patch.object(podmonitor, "apple_episodes", lambda *_: hints):
            self.assertEqual("", podmonitor.check())
        self.assertFalse(self.queue())

    def test_apple_lookup_failure_does_not_age_or_notify(self):
        self.check()
        self.feed = [episode("late", "Episode 152")] + self.feed
        self.check()
        self.clock += timedelta(days=4)
        with mock.patch.object(podmonitor, "feed_episodes", lambda *_: list(self.feed)), \
                mock.patch.object(podmonitor, "apple_episodes", side_effect=LookupError("Apple 503")):
            self.assertEqual("", podmonitor.check())
        entry = json.loads((self.state / "monitor-state.json").read_text())["1000000101"]
        self.assertIn("Apple 503", entry["last_error"])
        self.assertIn("late", entry["pending"])
        self.assertNotIn("late", entry.get("unresolved_notified", []))

    def test_rejected_queue_add_retains_pending(self):
        self.check()
        self.feed = [episode("late")] + self.feed
        self.apple = {"late": "https://podcasts.apple.com/gb/podcast/id1000000101?i=1000000000003"}
        with mock.patch.object(podqueue, "add", return_value="Not queued: invalid episode"):
            self.check()
        entry = json.loads((self.state / "monitor-state.json").read_text())["1000000101"]
        self.assertNotIn("late", entry["seen"])
        self.assertIn("invalid episode", entry["last_error"])


NEW_AUDIO = "https://cdn.example/new.mp3"


def link(track: int) -> str:
    return f"https://podcasts.apple.com/gb/podcast/id1000000101?i={track}"


def row(track: int, audio: str) -> dict:
    return {"wrapperType": "podcastEpisode", "trackId": track, "episodeGuid": f"apple-{track}", "episodeUrl": audio}


class AppleWindowTests(MonitorFixture):
    """Apple answers each lookup window separately, and they disagree: the recent and the pending
    window are read together, and a failed lookup decides nothing."""

    def setUp(self):
        super().setUp()
        self.windows: dict[int, list[dict]] = {10: [], 100: []}
        self.failing: set[tuple[str, int]] = set()
        self.body = b""
        self.queries: list[tuple[str, int]] = []

    def answer(self, url):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        store, limit = params["country"][0], int(params["limit"][0])
        self.queries.append((store, limit))
        if (store, limit) in self.failing:
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)
        rows = [{"wrapperType": "track", "kind": "podcast", "trackId": 1000000101}] + self.windows.get(limit, [])
        return 200, self.body or json.dumps({"results": rows}).encode(), {}

    def look(self) -> str:
        with mock.patch.object(podmonitor, "feed_episodes", lambda show, country: list(self.feed)), \
                mock.patch.object(podfetch, "http_get", side_effect=self.answer):
            return podmonitor.check()

    def entry(self) -> dict:
        return json.loads((self.state / "monitor-state.json").read_text())["1000000101"]

    def release(self) -> None:
        self.look()                                                       # first sight
        self.feed.insert(0, episode("rss-new", "Just published", enclosure=NEW_AUDIO))   # RSS GUID != Apple's

    def test_a_new_release_only_in_the_recent_window_queues_once(self):
        self.release()
        self.windows[10] = [row(1000000000007, NEW_AUDIO)]                # the larger answer lags: not in it
        self.assertEqual("", self.look())
        self.assertEqual("", self.look())
        self.assertEqual([link(1000000000007)], self.queue())             # matched by its audio, queued once
        self.assertEqual({("us", 10), ("us", 100), ("gb", 10), ("gb", 100)}, set(self.queries))
        self.assertIn("rss-new", self.entry()["seen"])

    def test_a_pending_episode_only_in_the_larger_window_recovers(self):
        self.release()
        self.look()
        self.assertIn("rss-new", self.entry()["pending"])                 # Apple has not listed it yet
        self.windows[100] = [row(1000000000008, NEW_AUDIO)]               # the recent answer lags this time
        self.look()
        self.assertEqual([link(1000000000008)], self.queue())
        self.assertNotIn("rss-new", self.entry()["pending"])

    def test_two_tracks_for_one_episode_across_windows_are_ambiguous(self):
        self.release()
        self.windows = {10: [row(1000000000005, NEW_AUDIO)], 100: [row(1000000000006, NEW_AUDIO)]}
        self.look()
        self.assertEqual([], self.queue())
        self.assertIn("rss-new", self.entry()["pending"])                 # kept, never discarded
        self.assertNotIn("rss-new", self.entry()["seen"])

    def test_a_failed_lookup_decides_nothing_and_recovers(self):
        self.release()
        self.windows[10] = [row(1000000000007, NEW_AUDIO)]
        self.failing = {("gb", 100)}                                      # one window of four fails
        self.clock += timedelta(days=4)
        self.assertEqual("", self.look())                                 # no "still unresolved" notice either
        self.assertEqual([], self.queue())
        self.assertIn("HTTP 503 (gb, 100 episodes)", self.entry()["last_error"])
        self.assertNotIn("rss-new", self.entry()["seen"])
        self.failing, self.body = set(), b"<html>busy</html>"             # an answer that is not one
        self.look()
        self.assertEqual([], self.queue())
        self.assertIn("unreadable answer", self.entry()["last_error"])
        self.body = b""
        self.look()
        self.assertEqual([link(1000000000007)], self.queue())
        self.assertNotIn("last_error", self.entry())

    def test_an_empty_answer_is_not_an_error(self):
        self.release()
        self.look()
        self.assertNotIn("last_error", self.entry())
        self.assertIn("rss-new", self.entry()["pending"])
