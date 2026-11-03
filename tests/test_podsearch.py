"""podsearch: phrase matching and periods in plain words."""
from __future__ import annotations

import unittest
from datetime import date

import podsearch


class SearchTests(unittest.TestCase):

    def test_titles_match_ignoring_case_and_punctuation(self):
        wanted = podsearch.words("Night Sky")
        for title in ("Night Sky #51 – Vera Lund", "NIGHT-SKY special", "Live from night sky"):
            self.assertIn(f" {wanted} ", f" {podsearch.words(title)} ")
        self.assertNotIn(f" {wanted} ", f" {podsearch.words('Nightsky viewing')} ")   # whole words only

    def test_periods_in_plain_words(self):
        today = date(2026, 10, 9)
        self.assertEqual((date(2026, 1, 1), today), podsearch.period("this year", today))
        self.assertEqual((date(2025, 1, 1), date(2025, 12, 31)), podsearch.period("last year", today))
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30)), podsearch.period("last month", today))
        self.assertEqual((date(2026, 9, 1), date(2026, 9, 30)), podsearch.period("2026-09", today))
        self.assertEqual((date(2026, 10, 5), today), podsearch.period("this week", today))
        self.assertEqual((date(2026, 9, 28), date(2026, 10, 4)), podsearch.period("last week", today))
        self.assertEqual((date(2026, 9, 25), today), podsearch.period("last 2 weeks", today))
        self.assertEqual((date(2026, 9, 1), today), podsearch.period("since 2026-09-01", today))
        self.assertEqual((date.min, date.max), podsearch.period("", today))
        with self.assertRaises(ValueError):
            podsearch.period("whenever", today)


from datetime import datetime, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

import podqueue  # noqa: E402
import podshows  # noqa: E402
from sources import youtube  # noqa: E402

CHANNEL = "UCexampleChannel000000Bb"
PLAYLIST = "PLexamplePlaylist0000000000000000A"
REGISTRY = {"1999999901": {"title": "Harbour Talk", "folder": "Harbour Talk", "country": "gb",
                           "sources": [{"type": "apple", "id": "1999999901", "country": "gb", "priority": 1},
                                       {"type": "youtube", "channel": CHANNEL, "playlist": PLAYLIST, "priority": 2}]},
            "yt:UCanotherChannel00000000Cc": {"title": "Η Νύχτα του Λιμανιού", "folder": "Η Νύχτα του Λιμανιού", "country": "gb"}}


def episode(title, when, guid):
    return SimpleNamespace(title=title, guid=guid, description="", duration="3600", published=datetime.fromisoformat(when).replace(tzinfo=timezone.utc))


def video(vid, title, seconds=3600):
    return {"id": vid, "title": title, "seconds": seconds, "signed_in": False}


INFOS = {"Xx0Test0Ep1": {"title": "Night Sky over the Harbour | Harbour Talk", "timestamp": 1790000000, "duration": 3600},
         "Xx0Test0Ep2": {"title": "Night Sky: the YouTube special", "timestamp": 1791000000, "duration": 3600},
         "Xx0Test0Ep3": {"title": "Night Sky trailer", "timestamp": 1791000000, "duration": 90},
         "Xx0Test0Ep4": {"title": "Night Sky in 2025", "timestamp": 1740000000, "duration": 3600}}


class YouTubeSearchTests(unittest.TestCase):

    def setUp(self):
        for target, name, value in ((podshows, "load", lambda: REGISTRY), (youtube, "video_info", lambda v, fresh=False: INFOS[v])):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.feed = [episode("Night Sky over the Harbour", "2026-09-21T13:00:00", "g1"),
                     episode("Tides and Trade", "2026-09-28T13:00:00", "g2")]
        self.videos = [video("Xx0Test0Ep4", "Night Sky in 2025"), video("Xx0Test0Ep3", "Night Sky trailer", 90),
                       video("Xx0Test0Ep2", "Night Sky: the YouTube special"), video("Xx0Test0Ep1", "Night Sky over the Harbour | Harbour Talk")]

    def run_search(self, show, **options):
        with mock.patch.object(podsearch.podmonitor, "feed_episodes", return_value=self.feed), \
                mock.patch.object(youtube, "recent_videos", return_value=self.videos) as listing, \
                mock.patch.object(podsearch.podmonitor, "apple_episodes", return_value=[]), \
                mock.patch.object(podsearch.podmonitor, "matched_links", return_value={"g1": "https://podcasts.apple.com/gb/podcast/id1999999901?i=1"}), \
                mock.patch.object(podqueue, "add", return_value="Queued (position 1 of 1). Language: English.") as add:
            said = podsearch.search(show, "night sky", **options)
        return said, listing, add

    def test_any_script_matches(self):
        self.assertIn(f" {podsearch.words('νυχτα')} ", f" {podsearch.words('Η ΝΎΧΤΑ του λιμανιού')} ")
        self.assertIn(f" {podsearch.words('ночь')} ", f" {podsearch.words('Тёмная НОЧЬ')} ")

    def test_a_show_on_both_sources_is_searched_on_both_and_each_episode_listed_once(self):
        said, listing, _ = self.run_search("https://podcasts.apple.com/gb/podcast/harbour-talk/id1999999901",
                                           when="2026", dry_run=True)
        self.assertEqual(((CHANNEL, PLAYLIST), {"count": podsearch.LISTING_LIMIT}), (listing.call_args.args, listing.call_args.kwargs))
        self.assertEqual(['2 episodes match "night sky", 2026:',
                          "2026-09-21 — Night Sky over the Harbour [Apple] (also on YouTube)",
                          "2026-10-03 — Night Sky: the YouTube special [YouTube]"], said.splitlines())

    def test_youtube_matches_are_queued_by_their_watch_links(self):
        said, _, add = self.run_search("Harbour Talk", when="2026", language="English")
        self.assertEqual([("https://podcasts.apple.com/gb/podcast/id1999999901?i=1",), ("https://www.youtube.com/watch?v=Xx0Test0Ep2",)],
                         [c.args for c in add.call_args_list])
        self.assertEqual(("search", "English"), (add.call_args.kwargs["source"], add.call_args.kwargs["language"]))
        self.assertEqual("Night Sky: the YouTube special", add.call_args.kwargs["peek"]["title"])   # no second lookup
        self.assertTrue(said.startswith('2 episodes match "night sky", 2026: 2 queued, 0 skipped.'))

    def test_a_youtube_only_show_by_link_or_name(self):
        self.feed = []
        with mock.patch.object(youtube, "channel", return_value={"channel": "UCanotherChannel00000000Cc", "title": "x", "playlist": ""}):
            said, listing, _ = self.run_search("https://www.youtube.com/@harbournight", dry_run=True)
        self.assertEqual(("UCanotherChannel00000000Cc", ""), listing.call_args.args)
        self.assertNotIn("[YouTube]", said)                                    # one source: no labels
        self.assertIn("2025-02-19 — Night Sky in 2025", said)
        self.assertNotIn("trailer", said)                                      # a clip on a channel is passed over
        said, listing, _ = self.run_search("Νύχτα του Λιμανιού", dry_run=True)
        self.assertEqual(("UCanotherChannel00000000Cc", ""), listing.call_args.args)
        self.assertIn("Not a show link", podsearch.search("https://www.youtube.com/watch?v=Xx0Test0Ep1", "night"))

    def test_a_show_in_another_language_matches_on_its_original_titles(self):
        INFOS["Xx0Test0Ep5"] = {"title": "Η νύχτα στο λιμάνι", "timestamp": 1791000000, "duration": 3600}
        self.addCleanup(INFOS.pop, "Xx0Test0Ep5")
        english = [video("Xx0Test0Ep5", "The night at the harbour")]           # YouTube's machine translation
        native = [video("Xx0Test0Ep5", "Η νύχτα στο λιμάνι")]
        registry = {k: dict(v) for k, v in REGISTRY.items()}
        registry["yt:UCanotherChannel00000000Cc"]["language"] = "el"

        def listing(channel, playlist="", count=15, lang=""):
            return native if lang == "el" else english
        for phrase, show in (("νύχτα", "Νύχτα του Λιμανιού"), ("night at the harbour", "Νύχτα του Λιμανιού")):
            with mock.patch.object(podshows, "load", lambda: registry), mock.patch.object(youtube, "recent_videos", listing):
                said = podsearch.search(show, phrase, dry_run=True)
            self.assertIn("2026-10-03 — Η νύχτα στο λιμάνι", said, phrase)
        langs = []
        with mock.patch.object(youtube, "recent_videos", lambda *a, **k: langs.append(k.get("lang", "")) or english):
            podsearch.search("Νύχτα του Λιμανιού", "νύχτα", dry_run=True)       # no language known: the phrase's script says it
        self.assertEqual(["", "el"], langs)
