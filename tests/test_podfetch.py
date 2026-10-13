#!/usr/bin/env python3
"""Unit tests for podcast URL resolution and enclosure refresh."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import podfetch


APPLE_EPISODE_URL = "https://podcasts.apple.com/gb/podcast/example/id123?i=456"


class UnitTests(unittest.TestCase):

    def test_rejects_lookalike_host_and_non_https(self):
        with self.assertRaises(ValueError):
            podfetch.parse_apple_url("https://example.com/podcast/x/id123?i=456")
        with self.assertRaises(ValueError):
            podfetch.parse_apple_url("http://podcasts.apple.com/podcast/x/id123?i=456")





    def test_exact_apple_episode_url_precedes_fuzzy_title(self):
        episodes = [
            podfetch.Episode(
                guid="feed-guid",
                title="Publisher changed this title",
                enclosure_url="https://audio.example/exact.mp3",
            )
        ]
        result = podfetch.match_episode(
            episodes,
            guid="different-apple-guid",
            enclosure_url="https://audio.example/exact.mp3",
            title="Completely different Apple title",
        )
        self.assertEqual(result.guid, "feed-guid")
        self.assertEqual(result.match_method, "apple_episode_url")




    def test_new_episode_retries_once_without_stale_conditional_feed_cache(self):
        stale = b"""<rss><channel><title>Fixture</title><item>
        <title>Completely unrelated briefing</title><guid>old</guid>
        </item></channel></rss>"""
        fresh = b"""<rss><channel><title>Fixture</title><item>
        <title>New episode</title><guid>new-guid</guid>
        <pubDate>Tue, 01 Sep 2026 17:00:00 +0000</pubDate>
        <enclosure url='https://audio.example/new.mp3' type='audio/mpeg'/>
        </item></channel></rss>"""
        hint = podfetch.AppleEpisodeHint(
            track_id="456", title="New episode", episode_guid="new-guid",
            episode_url="https://audio.example/new.mp3",
        )
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(podfetch, "resolve_feed_url_cached", return_value="https://example.test/feed.xml"), \
             mock.patch.object(podfetch, "fetch_feed_cached", side_effect=[stale, fresh]) as fetch, \
             mock.patch.object(podfetch, "episode_hint_from_apple", return_value=hint):
            _show, episode = podfetch.resolve_apple_url(
                "https://podcasts.apple.com/gb/podcast/x/id123?i=456", cache_dir=directory,
            )
        self.assertEqual("new-guid", episode.guid)
        self.assertTrue(fetch.call_args_list[1].kwargs["force_refresh"])

    def test_stale_or_expired_enclosure_requires_refresh(self):
        now = datetime(2026, 8, 28, tzinfo=timezone.utc)
        self.assertTrue(podfetch.enclosure_needs_refresh(now - timedelta(days=1), now=now))
        self.assertFalse(podfetch.enclosure_needs_refresh(now - timedelta(hours=23), now=now))
        self.assertTrue(podfetch.enclosure_needs_refresh(now, response_status=403, now=now))
        self.assertTrue(podfetch.enclosure_needs_refresh(now, response_status=404, now=now))
        refreshed = podfetch.Episode(guid="new", enclosure_url="https://audio.example/new.mp3")
        with mock.patch.object(
            podfetch,
            "resolve_apple_url",
            return_value=(podfetch.Show(title="Fixture"), refreshed),
        ) as resolver:
            result = podfetch.refresh_enclosure_if_needed(
                APPLE_EPISODE_URL,
                now - timedelta(days=2),
                cache_dir="/tmp/disposable-resolver-cache",
                now=now,
            )
        self.assertIs(result, refreshed)
        self.assertTrue(resolver.call_args.kwargs["force_feed_refresh"])




if __name__ == "__main__":
    unittest.main(verbosity=2)
