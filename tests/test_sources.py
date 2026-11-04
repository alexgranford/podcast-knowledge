#!/usr/bin/env python3
"""Unit tests for the episode sources: links, YouTube's facts and errors, ranged download, one episode on
two sources (queue, library, podnote, monitor). No network: yt-dlp and the downloads are stubbed."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import audio_acquisition
import notecheck
import podmonitor
from datetime import datetime, timezone
from types import SimpleNamespace
import podnote
import podqueue
import podshows
import sources
from sources import youtube

VIDEO = "Xx0Test0Ep2"
CHANNEL = "UCexampleChannel000000Bb"
PLAYLIST = "PLexamplePlaylist0000000000000000A"
APPLE = "https://podcasts.apple.com/gb/podcast/id1999999901?i=1000000099448"
INFO = {"id": VIDEO, "title": "Why the Tide Will Turn – Sam Rivers", "channel": "Harbour Talk", "channel_id": CHANNEL,
        "uploader": "Harbour Talk", "timestamp": 1793473208, "duration": 6016, "live_status": "not_live",
        "availability": "public", "format_id": "140", "vcodec": "none",
        "url": "https://rr1---sn-x.googlevideo.com/videoplayback?expire=1&sig=a",
        "description": "Thanks to the sponsors below for making that possible:\n\n"
                       "- Take Lakeside College’s online courses for free at https://lakeside.edu/harbour\n\n"
                       "Sam Rivers joins us.\n\n00:00 Trailer\n01:01 The Abundance Question\n1:02:03 - Harbour Plans\n",
        "chapters": [{"start_time": 0, "title": "Trailer"}, {"start_time": 61, "title": "The Abundance Question"}]}


class LinkTests(unittest.TestCase):

    def test_youtube_link_forms_give_one_canonical_video(self):
        for link in (f"https://www.youtube.com/watch?v={VIDEO}", f"https://youtu.be/{VIDEO}?t=10",
                     f"https://m.youtube.com/watch?v={VIDEO}&list=PLx", f"https://music.youtube.com/watch?v={VIDEO}",
                     f"https://www.youtube.com/shorts/{VIDEO}", f"https://www.youtube.com/live/{VIDEO}",
                     f"http://youtube.com/embed/{VIDEO}"):
            ref = sources.episode_ref(link)
            self.assertEqual(("youtube", VIDEO, f"https://www.youtube.com/watch?v={VIDEO}", f"youtube-{VIDEO}"),
                             (ref.source, ref.episode, ref.url, ref.job_id), link)

    def test_lookalikes_credentials_and_bad_ids_are_refused(self):
        for link in (f"https://youtube.com.evil.example/watch?v={VIDEO}", f"https://user@www.youtube.com/watch?v={VIDEO}",
                     "https://www.youtube.com/watch?v=short", f"https://www.youtube.com/watch?v={VIDEO}x",
                     f"ftp://www.youtube.com/watch?v={VIDEO}", "https://www.youtube.com/feed/library"):
            with self.assertRaises(ValueError, msg=link):
                sources.episode_ref(link)

    def test_channel_and_playlist_links_are_shows_not_episodes(self):
        self.assertEqual(CHANNEL, sources.parse(f"https://www.youtube.com/channel/{CHANNEL}/videos").show)
        self.assertEqual("@harbourtalk", sources.parse("https://www.youtube.com/@harbourtalk").show)
        self.assertEqual(PLAYLIST, sources.parse(f"https://www.youtube.com/playlist?list={PLAYLIST}").show)
        with self.assertRaisesRegex(ValueError, "channel or playlist link"):
            sources.episode_ref("https://www.youtube.com/@harbourtalk")

    def test_share_and_tracking_parameters_never_survive(self):
        cases = {
            f"https://youtu.be/{VIDEO}?si=AbCdEfGhIjKlMnOp": f"https://www.youtube.com/watch?v={VIDEO}",
            f"https://youtu.be/{VIDEO}?is=GY5oFVb6Cmb-KHB2": f"https://www.youtube.com/watch?v={VIDEO}",
            f"https://m.youtube.com/watch?v={VIDEO}&si=x&pp=y&feature=shared&t=30": f"https://www.youtube.com/watch?v={VIDEO}",
            f"https://www.youtube.com/live/{VIDEO}?si=x": f"https://www.youtube.com/watch?v={VIDEO}",
            "https://podcasts.apple.com/gb/podcast/harbour-talk/id1999999901?i=1000000099448&uo=4&utm_source=share": APPLE,
        }
        for sent, clean in cases.items():
            self.assertEqual(clean, sources.episode_ref(sent).url, sent)
        for channel in ("https://www.youtube.com/@harbourtalk?si=x", f"https://www.youtube.com/channel/{CHANNEL}?si=x",
                        f"https://www.youtube.com/playlist?list={PLAYLIST}&si=x"):
            self.assertNotIn("si=", sources.parse(channel).url, channel)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(podqueue, "STATE", Path(directory)):
            podqueue.add(f"https://youtu.be/{VIDEO}?si=AbCdEfGhIjKlMnOp", "chat")
            self.assertEqual([f"https://www.youtube.com/watch?v={VIDEO}"], podqueue.read_lines(Path(directory) / "queue.txt"))

    def test_apple_links_keep_their_job_ids(self):
        ref = sources.episode_ref(APPLE)
        self.assertEqual(("apple", "1000000099448", "1999999901", "gb", "podcast-1000000099448", APPLE),
                         (ref.source, ref.episode, ref.show, ref.country, ref.job_id, ref.url))
        with self.assertRaisesRegex(ValueError, "show link"):
            sources.episode_ref("https://podcasts.apple.com/gb/podcast/x/id1999999901")

    def test_every_source_s_job_id_passes_the_transcript_contract(self):
        import re
        schema = json.loads((Path(__file__).resolve().parents[1] / "references" / "schemas"
                             / "ordered-transcript-v1.schema.json").read_text(encoding="utf-8"))
        pattern = schema["properties"]["job_id"]["pattern"]
        for connector in sources.CONNECTORS.values():
            self.assertRegex(f"{connector.JOB_PREFIX}-{VIDEO}-asr-20261102-065012", pattern, connector.NAME)
        self.assertIsNone(re.search(pattern, "other-1"))

    def test_a_note_points_to_its_job(self):
        cache = Path("/cache")
        self.assertEqual(cache / "jobs" / f"youtube-{VIDEO}", sources.job_root(cache, f"---\nyoutube_video_id: '{VIDEO}'\n---\n"))
        self.assertEqual(cache / "jobs" / "podcast-12", sources.job_root(cache, "---\napple_episode_id: \"12\"\n---\n"))
        self.assertIsNone(sources.job_root(cache, "---\nshow: x\n---\n"))


class YouTubeFactsTests(unittest.TestCase):

    def resolve(self, info: dict, registry: dict | None = None) -> tuple[dict, dict]:
        with mock.patch.object(youtube, "video_info", return_value=info), \
                mock.patch.object(podshows, "load", return_value=registry or {}):
            return youtube.resolve(sources.episode_ref(f"https://youtu.be/{VIDEO}"), Path("/cache"))

    def test_record_and_credits(self):
        record, credits = self.resolve(INFO)
        self.assertEqual({"source_url": f"https://www.youtube.com/watch?v={VIDEO}", "youtube_video_id": VIDEO,
                          "show": "Harbour Talk", "episode": INFO["title"], "published": "2026-10-31",
                          "duration_seconds": 6016}, {k: v for k, v in record.items() if k not in ("episode_description", "chapters")})
        self.assertEqual({"show_author": "Harbour Talk", "youtube_channel_id": CHANNEL, "youtube_show": f"yt:{CHANNEL}",
                          "published_utc": "2026-10-31T19:00:08"}, credits)
        self.assertEqual([{"start": 0, "title": "Trailer"}, {"start": 61, "title": "The Abundance Question"}], record["chapters"])
        self.assertNotIn("Ezra Klein", record["episode_description"])     # chapter lines are not names for the hint
        self.assertNotIn("Harbour Plans", record["episode_description"])
        self.assertIn("Sam Rivers joins us.", record["episode_description"])
        self.assertIn("Lakeside College", podnote.sponsors_in(record["episode_description"]))

    def test_a_linked_channel_takes_the_registry_show(self):
        registry = {"1999999901": {"title": "Harbour Talk", "folder": "Harbour Talk", "sources": [
            {"type": "apple", "id": "1999999901", "priority": 1}, {"type": "youtube", "channel": CHANNEL, "playlist": "", "priority": 2}]}}
        record, _ = self.resolve(INFO, registry)
        self.assertEqual("Harbour Talk", record["show"])
        with mock.patch.object(podshows, "load", return_value=registry):
            self.assertEqual(("1999999901", "gb"), podnote.show_key({"youtube_channel_id": CHANNEL}))
            self.assertEqual((f"yt:UC{'x' * 22}", "gb"), podnote.show_key({"youtube_channel_id": f"UC{'x' * 22}"}))

    def test_a_missing_time_is_left_out_not_invented(self):
        info = {k: v for k, v in INFO.items() if k not in ("timestamp", "chapters")} | {"upload_date": "20261031"}
        record, credits = self.resolve(info)
        self.assertEqual(("2026-10-31", ""), (record["published"], credits["published_utc"]))
        self.assertNotIn("chapters", record)
        self.assertIn("00:00 Trailer", record["episode_description"])   # no chapters as data: the text keeps them

    def test_errors_in_plain_words_and_retry_marks(self):
        rows = {
            "ERROR: [youtube] x: Sign in to confirm you’re not a bot. Use --cookies": ("bot check", True),
            "ERROR: [youtube] x: Premieres in 2 hours": ("not started", True),
            "ERROR: [youtube] x: Video unavailable. This video is private": ("unavailable", False),
            "ERROR: [youtube] x: Private video. Sign in if you've been granted access": ("private", False),
            "ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate": ("age-restricted", False),
            "ERROR: [youtube] x: Join this channel to get access to members-only content": ("members only", False),
            "ERROR: [youtube] x: Unable to extract nsig function code": ("needs an update", True),
            "ERROR: Unable to download API page: <urlopen error [Errno -3] Temporary failure>": ("could not be reached", True),
            "ERROR: [youtube] x: Something new": ("yt-dlp failed: Something new", False),
        }
        for stderr, (words, retried) in rows.items():
            error = youtube.explain("WARNING: noise\n" + stderr)
            self.assertIn(words, str(error), stderr)
            self.assertEqual(retried, error.retry, str(error))
            self.assertTrue(podqueue.plain_reason(str(error)).startswith(("YouTube", "yt-dlp")), str(error))

    def test_without_ytdlp_youtube_is_off_and_links_are_refused(self):
        with mock.patch.dict("os.environ", {"PODNOTE_YTDLP": ""}):
            with self.assertRaisesRegex(sources.SourceError, "switched off"):
                youtube.ytdlp(["--version"])
            with tempfile.TemporaryDirectory() as directory, mock.patch.object(podqueue, "STATE", Path(directory)):
                self.assertTrue(podqueue.add(f"https://youtu.be/{VIDEO}", "chat").startswith("Not queued: YouTube links are switched off"))
                self.assertEqual([], podqueue.read_lines(Path(directory) / "queue.txt"))

    def test_live_and_signed_in_videos_are_refused_before_download(self):
        for change, words, retry in (({"live_status": "is_upcoming"}, "try again later", True),
                                     ({"availability": "subscriber_only"}, "signed-in", False)):
            youtube._RESOLVED.clear()
            with mock.patch.object(youtube, "ytdlp", return_value=INFO | change), \
                    self.assertRaisesRegex(sources.SourceError, words) as raised:
                youtube.video_info(VIDEO)
            self.assertEqual(retry, raised.exception.retry)
        youtube._RESOLVED.clear()

    def test_only_googlevideo_audio_streams_are_fetched(self):
        self.assertTrue(youtube.stream_url(INFO).startswith("https://rr1---sn-x.googlevideo.com/"))
        for bad in ({"url": "https://evil.example/videoplayback"}, {"url": "http://rr1.googlevideo.com/v"}, {"vcodec": "avc1"}):
            with self.assertRaises(sources.SourceError):
                youtube.stream_url(INFO | bad)


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict):
        self.status, self.body, self.headers = status, body, headers

    def getheader(self, name: str, default=None):
        return self.headers.get(name, default)

    def read(self, size: int = -1) -> bytes:
        chunk, self.body = (self.body, b"") if size < 0 else (self.body[:size], self.body[size:])
        return chunk


class EpisodeRecordTests(unittest.TestCase):

    def test_every_source_builds_the_same_episode_record(self):
        from types import SimpleNamespace
        shape = set(sources.episode("1999999901", "apple", "Ep"))
        item = SimpleNamespace(title="Ep", published=None, duration="01:00:00", description="About it.")
        self.assertEqual(shape, set(sources.apple.peek_from_feed(item, "1999999901")))
        self.assertEqual(shape, set(youtube.peek_from_info(INFO, "1999999901")))
        self.assertEqual(3600, sources.apple.peek_from_feed(item, "1999999901")["seconds"])

class RangeDownloadTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.target = Path(directory.name) / "episode-audio"
        self.data = bytes(range(256)) * 100                   # 25,600 bytes
        self.ranges: list[str] = []
        self.hosts: list[str] = []

    def connection(self, statuses: dict | None = None):
        test = self

        class Connection:
            def __init__(self, host, *args):
                self.host = host

            def request(self, method, path, headers):
                test.ranges.append(headers["Range"])
                test.hosts.append(self.host)
                start, end = (int(n) for n in headers["Range"][6:].split("-"))
                self.response = FakeResponse(206, test.data[start:end + 1], {
                    "Content-Type": "audio/mp4", "Content-Range": f"bytes {start}-{min(end, len(test.data) - 1)}/{len(test.data)}"})
                if statuses and len(test.ranges) in statuses:
                    status = statuses[len(test.ranges)]
                    status, location = status if isinstance(status, tuple) else (status, None)
                    self.response = FakeResponse(status, b"", {"Location": location} if location else {})

            def getresponse(self):
                return self.response

            def close(self):
                pass
        return Connection

    STREAM = "https://rr1---sn-a.googlevideo.com/videoplayback?id=o-abc&itag=140&clen=25600&mime=audio%2Fmp4&sig=1"

    def download(self, statuses: dict | None = None, **options) -> dict:
        with mock.patch.object(audio_acquisition, "public_endpoint", lambda url: (__import__("urllib.parse").parse.urlparse(url), "203.0.113.5")), \
                mock.patch.object(audio_acquisition, "PinnedHTTPSConnection", self.connection(statuses)):
            return audio_acquisition.acquire_audio(self.STREAM, self.target, range_bytes=10_000, **options)

    def guarded(self, statuses: dict | None = None, **options) -> dict:
        return self.download(statuses, same_stream=lambda location: youtube.same_stream(self.STREAM, location), **options)

    def test_a_redirect_to_the_same_stream_on_another_server_is_followed(self):
        moved = self.STREAM.replace("rr1---sn-a", "rr5---sn-b") + "&rdr=1"
        result = self.guarded({2: (302, moved)}, expected_bytes=len(self.data))
        self.assertEqual(["rr1---sn-a.googlevideo.com", "rr1---sn-a.googlevideo.com", "rr5---sn-b.googlevideo.com",
                          "rr5---sn-b.googlevideo.com"], self.hosts)            # later ranges go to the new server
        self.assertEqual(self.data, self.target.read_bytes())
        self.assertEqual(moved, result["final_url"])

    def test_a_redirect_anywhere_else_is_not_followed(self):
        for location in ("https://ads.example.com/videoplayback?id=o-abc&itag=140&clen=25600&mime=audio%2Fmp4",
                         self.STREAM.replace("id=o-abc", "id=o-other"), self.STREAM.replace("itag=140", "itag=251"),
                         self.STREAM.replace("clen=25600", "clen=999"), self.STREAM.replace("/videoplayback", "/ad"),
                         self.STREAM.replace("https:", "http:")):
            self.ranges.clear()
            with self.assertRaisesRegex(audio_acquisition.AudioTransientError, "not the same stream; not followed"):
                self.guarded({1: (302, location)})
            self.assertEqual(1, len(self.ranges), location)
        self.ranges.clear()
        with self.assertRaisesRegex(audio_acquisition.AudioTransientError, "not the same stream"):
            self.download({1: (302, self.STREAM)})                  # with no stream check, no redirect is followed

    def test_a_size_other_than_the_stream_s_is_refused(self):
        with self.assertRaisesRegex(audio_acquisition.AudioTransientError, "not the stream's 30000"):
            self.guarded(expected_bytes=30_000)
        self.assertFalse(self.target.with_suffix(".part").exists())
        self.assertFalse(self.target.exists())

    def test_ranges_until_the_total_then_published(self):
        result = self.download()
        self.assertEqual(["bytes=0-9999", "bytes=10000-19999", "bytes=20000-29999"], self.ranges)
        self.assertEqual(self.data, self.target.read_bytes())
        self.assertEqual(len(self.data), result["bytes"])
        self.assertFalse(self.target.with_suffix(".part").exists())

    def test_a_failed_range_keeps_the_part_and_the_next_run_resumes(self):
        with self.assertRaisesRegex(audio_acquisition.AudioError, "HTTP 403"):
            self.download({2: 403})
        self.assertEqual(10_000, self.target.with_suffix(".part").stat().st_size)
        self.ranges.clear()
        self.download()
        self.assertEqual(["bytes=10000-19999", "bytes=20000-29999"], self.ranges)
        self.assertEqual(self.data, self.target.read_bytes())

    def test_a_server_that_ignores_ranges_sends_the_whole_file_once(self):
        test = self

        class WholeFile:
            def __init__(self, host, *args):
                pass

            def request(self, method, path, headers):
                test.ranges.append(headers["Range"])
                self.response = FakeResponse(200, test.data, {"Content-Type": "audio/mp4"})

            def getresponse(self):
                return self.response

            def close(self):
                pass
        with mock.patch.object(audio_acquisition, "public_endpoint", lambda url: (__import__("urllib.parse").parse.urlparse(url), "203.0.113.5")), \
                mock.patch.object(audio_acquisition, "PinnedHTTPSConnection", WholeFile):
            result = audio_acquisition.acquire_audio(self.STREAM, self.target, range_bytes=10_000, expected_bytes=len(self.data))
        self.assertEqual(["bytes=0-9999"], self.ranges)                    # one answer held it all
        self.assertEqual((self.data, len(self.data)), (self.target.read_bytes(), result["bytes"]))

    def test_a_server_error_is_transient(self):
        with self.assertRaises(audio_acquisition.AudioTransientError):
            self.download({1: 503})


class TwinFixture(unittest.TestCase):
    """A show on both sources, its folder and a registry; no tests of its own."""

    """One episode published on Apple and on YouTube is processed once."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.notes, self.state = root / "notes", root / "state"
        self.folder = self.notes / "Podcasts" / "Harbour Talk"
        self.folder.mkdir(parents=True)
        for module, name, value in ((podqueue, "STATE", self.state), (podqueue, "ROOT", self.notes),
                                    (podqueue, "EPISODES", Path("Podcasts")), (podshows, "STATE", self.state),
                                    (podnote, "ROOT", self.notes), (podnote, "EPISODES", Path("Podcasts")),
                                    (podnote, "CACHE", root / "cache"), (podnote, "STATE", self.state)):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state.mkdir()
        (self.state / "shows.json").write_text(json.dumps({"1999999901": {
            "title": "Harbour Talk", "folder": "Harbour Talk", "country": "gb", "followers": ["admin"], "hosts": [],
            "fixes": {}, "advertisers": {}, "sources": [{"type": "apple", "id": "1999999901", "country": "gb", "priority": 1}, {"type": "youtube", "channel": CHANNEL, "playlist": PLAYLIST, "priority": 2}]}}), encoding="utf-8")
        self.apple = {"show": "1999999901", "title": "Why the Tide Will Turn – Sam Rivers", "published": "2026-10-31T19:00:00"}
        self.video = {"show": "1999999901", "title": "“Why the Tide Will Turn” – Sam Rivers | Harbour Talk",
                      "published": "2026-10-31T19:00:08"}


class TwinEpisodeTests(TwinFixture):

    def test_two_episodes_of_one_source_with_one_title_are_both_queued(self):
        other = "https://podcasts.apple.com/gb/podcast/id1999999901?i=1000000099449"
        for link in (APPLE, other):
            self.assertTrue(podqueue.add(link, "monitor", peek=dict(self.apple)).startswith("Queued"))
        self.assertEqual([APPLE, other], podqueue.read_lines(self.state / "queue.txt"))

    def test_two_candidates_with_the_title_leave_the_match_in_doubt(self):
        one, two = {**self.apple, "source": "apple", "id": "1"}, {**self.apple, "source": "apple", "id": "2"}
        self.assertIsNone(sources.twin_among({**self.video, "source": "youtube"}, [("a", one), ("b", two)], "Harbour Talk"))
        self.assertEqual("a", sources.twin_among({**self.video, "source": "youtube"}, [("a", one)], "Harbour Talk"))

    def test_same_show_title_and_time_is_one_episode(self):
        self.assertEqual("title", sources.match(self.apple, self.video, "Harbour Talk"))
        for other in ({"title": "Why the Tide Will Turn – Sam Rivers Part 2"}, {"published": "2026-11-03T19:00:00"},
                      {"show": "yt:" + CHANNEL}):
            self.assertEqual("", sources.match(self.apple, self.video | other, "Harbour Talk"), other)

    def test_a_copy_titled_differently_is_one_episode_by_time_and_length(self):
        apple = self.apple | {"published": "2026-10-29T15:00:00", "seconds": 2517, "source": "apple"}
        video = {"show": "1999999901", "title": "They're Lying To You About The Tide", "published": "2026-10-29T15:00:11",
                 "seconds": 2390, "source": "youtube"}                    # 11 s later, 5% shorter (no ads)
        self.assertEqual("time", sources.match(apple, video, "Harbour Talk"))
        for other, why in (({"published": "2026-10-29T15:26:00"}, "26 minutes apart"),
                           ({"seconds": 1800}, "a different length"),
                           ({"source": "apple"}, "two episodes from one source"),
                           ({"published": "2026-10-29"}, "a date without a time"),
                           ({"seconds": 0}, "no length")):
            self.assertEqual("", sources.match(apple, video | other, "Harbour Talk"), why)

    def test_two_close_candidates_are_neither_but_a_title_still_decides(self):
        video = {"show": "1999999901", "title": "Live", "published": "2026-10-29T15:00:00", "seconds": 600, "source": "youtube"}
        first = {"show": "1999999901", "title": "Part 1", "published": "2026-10-29T15:00:00", "seconds": 600, "source": "apple"}
        second = first | {"title": "Part 2", "published": "2026-10-29T15:04:00"}
        self.assertEqual("one", sources.twin_among(video, [("one", first)]))
        self.assertIsNone(sources.twin_among(video, [("one", first), ("two", second)]))   # a doubt: made, not lost
        self.assertEqual("two", sources.twin_among(video | {"title": "Part 2"}, [("one", first), ("two", second)]))

    def test_the_queue_and_the_library_see_a_differently_titled_copy(self):
        (self.folder / "2026-10-29 - Five Murders.md").write_text(
            '---\nepisode: "Five Murders"\npublished: "2026-10-29T15:00:00"\nduration: 2517\n'
            "apple_episode_id: '1000000099448'\n---\n# Five Murders\n", encoding="utf-8")
        video = {"show": "1999999901", "title": "They're Lying To You", "published": "2026-10-29T15:00:11",
                 "seconds": 2390, "source": "youtube"}
        self.assertEqual("library", podqueue.twin_status(video))
        self.assertEqual("", podqueue.twin_status(video | {"source": "apple"}))     # another Apple episode: kept
        self.assertTrue(podqueue.add(f"https://www.youtube.com/watch?v={VIDEO}", "monitor", peek=video)
                        .startswith("Already in the library"))

    def test_a_copy_close_to_one_made_and_one_queued_episode_is_in_doubt(self):
        (self.folder / "2026-11-03 - Part One.md").write_text(
            '---\nepisode: "Part One"\npublished: "2026-11-03T10:00:00"\nduration: 3600\n'
            "apple_episode_id: '1000000099001'\n---\n# Part One\n", encoding="utf-8")
        podqueue.add(APPLE, "monitor", peek={"show": "1999999901", "title": "Part Two", "published": "2026-11-03T10:08:00",
                                              "seconds": 3600})
        video = {"show": "1999999901", "title": "Another name", "published": "2026-11-03T10:05:00", "seconds": 3600,
                 "source": "youtube"}
        self.assertEqual("", podqueue.twin_status(video))                       # two fit: neither is taken

    def test_a_queued_link_and_its_own_note_are_one_candidate(self):
        podqueue.add(APPLE, "monitor", peek=self.apple)
        apple_id = APPLE.rsplit("=", 1)[1]
        (self.folder / "2026-10-31 - Own.md").write_text(
            f'---\nepisode: "{self.apple["title"]}"\npublished: "{self.apple["published"]}"\n'
            f"apple_episode_id: '{apple_id}'\n---\n", encoding="utf-8")
        self.assertEqual("library", podqueue.twin_status(self.video))

    def test_the_second_source_is_not_queued_while_the_first_waits(self):
        self.assertTrue(podqueue.add(APPLE, "monitor", peek=self.apple).startswith("Queued (position 1"))
        said = podqueue.add(f"https://www.youtube.com/watch?v={VIDEO}", "monitor", peek=self.video)
        self.assertEqual("Already queued (position 1 of 1, from another source).", said)
        self.assertEqual([APPLE], podqueue.read_lines(self.state / "queue.txt"))

    def test_the_second_source_is_not_queued_once_noted(self):
        (self.folder / "2026-10-31 - Why the Tide Will Turn – Sam Rivers.md").write_text(
            '---\nepisode: "Why the Tide Will Turn – Sam Rivers"\npublished: "2026-10-31T19:00:00"\n'
            "apple_episode_id: '1000000099448'\n---\n", encoding="utf-8")
        said = podqueue.add(f"https://www.youtube.com/watch?v={VIDEO}", "monitor", peek=self.video)
        self.assertEqual("Already in the library; not queued (from another source: add --force to make this one too).", said)
        self.assertIn("Already in the library", podqueue.add(APPLE))          # the same-source rule, as ever
        forced = podqueue.add(f"https://www.youtube.com/watch?v={VIDEO}", "chat", peek=self.video, force=True)
        self.assertTrue(forced.startswith("Queued ("), forced)                 # the user's override
        entry = podqueue.load_state()[f"https://www.youtube.com/watch?v={VIDEO}"]
        self.assertTrue(entry["force"])
        with mock.patch.object(podqueue, "run_owned", return_value=(1, "STOPPED: x")) as runner, \
                mock.patch.object(podqueue, "run_verify"):
            podqueue.run()
        self.assertIn("--force", runner.call_args.args[0])                    # podnote makes it regardless

    def test_a_link_sent_by_hand_is_looked_up_when_a_twin_may_exist(self):
        (self.folder / "2026-10-31 - Why the Tide Will Turn – Sam Rivers.md").write_text(
            '---\nepisode: "Why the Tide Will Turn – Sam Rivers"\npublished: "2026-10-31T19:00:00"\n'
            "apple_episode_id: '1000000099448'\n---\n", encoding="utf-8")
        with mock.patch.object(youtube, "peek", return_value=self.video) as looked:
            self.assertEqual("Already in the library; not queued (from another source: add --force to make this one too).", podqueue.add(f"https://youtu.be/{VIDEO}", "chat"))
        looked.assert_called_once()
        unlinked = "https://podcasts.apple.com/gb/podcast/x/id999?i=1000000000001"      # not linked, language settled
        registry = json.loads((self.state / "shows.json").read_text())
        registry["999"] = {"title": "X", "folder": "X", "hosts": [], "fixes": {}, "advertisers": {}, "language": "en"}
        (self.state / "shows.json").write_text(json.dumps(registry), encoding="utf-8")
        with mock.patch.object(sources.apple, "peek") as apple_peek:
            self.assertTrue(podqueue.add(unlinked, "chat").startswith("Queued"))
        apple_peek.assert_not_called()
        with mock.patch.object(youtube, "peek", return_value=None):                      # YouTube cannot say now
            self.assertTrue(podqueue.add("https://youtu.be/bbbbbbbbbbb", "chat").startswith("Queued"))

    def test_podnote_stops_before_downloading_a_twin(self):
        note = self.folder / "2026-10-31 - Why the Tide Will Turn – Sam Rivers.md"
        note.write_text('---\nepisode: "Why the Tide Will Turn – Sam Rivers"\napple_episode_id: \'1\'\n---\n', encoding="utf-8")
        record = {"source_url": f"https://www.youtube.com/watch?v={VIDEO}", "youtube_video_id": VIDEO, "show": "Harbour Talk",
                  "episode": self.video["title"], "published": "2026-10-31", "duration_seconds": 6016, "episode_description": ""}
        credits = {"show_author": "Harbour Talk", "youtube_channel_id": CHANNEL, "published_utc": "2026-10-31T19:00:08"}
        with mock.patch.object(youtube, "resolve", return_value=(record, credits)), \
                mock.patch.object(youtube, "fetch_audio") as fetch, self.assertRaises(podnote.Twin) as stopped:
            podnote.front_half(f"https://youtu.be/{VIDEO}", twin_check=True)
        self.assertEqual(str(note), str(stopped.exception))
        fetch.assert_not_called()
        note.unlink()                                                        # no twin: on to the download
        with mock.patch.object(youtube, "resolve", return_value=(record, credits)), \
                mock.patch.object(youtube, "fetch_audio", side_effect=RuntimeError("downloading")), \
                self.assertRaisesRegex(RuntimeError, "downloading"):
            podnote.front_half(f"https://youtu.be/{VIDEO}", twin_check=True)

    def test_the_youtube_note_names_its_source(self):
        root = Path(podnote.CACHE) / "jobs" / f"youtube-{VIDEO}"
        (root / "source").mkdir(parents=True)
        (root / "asr").mkdir()
        (root / "source" / "resolved-episode-v1.json").write_text(json.dumps({
            "source_url": f"https://www.youtube.com/watch?v={VIDEO}", "youtube_video_id": VIDEO, "show": "Harbour Talk",
            "episode": "Dems", "published": "2026-10-31", "duration_seconds": 6016, "episode_description": "",
            "youtube_channel_id": CHANNEL, "published_utc": "2026-10-31T19:00:08"}), encoding="utf-8")
        (root / "asr" / "transcript-v1.json").write_text(json.dumps({"chunks": [{"text": "Hello there."}]}), encoding="utf-8")

        def ask(base, headers, prompt, limit, system=""):
            return ("**Participants:** **Anna Smith** (host)\n\nA paragraph." if "Participants" in prompt
                    else "- A point.", "stop")
        with mock.patch.object(podnote, "ask", ask), mock.patch.object(podnote, "log"):
            note, problems = podnote.synthesise(root, "http://model", {})
        self.assertEqual([], problems)
        self.assertIn(f"youtube_video_id: '{VIDEO}'\n", note)
        self.assertNotIn("apple_episode_id", note)
        self.assertIn(f"▶️ [YouTube](https://www.youtube.com/watch?v={VIDEO})", note)
        self.assertEqual(root, sources.job_root(podnote.CACHE, note))
        self.assertEqual([], [i for i in notecheck.validate_note(note, set(), set(), profile=notecheck.EPISODE_PROFILE)
                              if i.get("field") == "youtube_video_id"])
        self.assertEqual(self.folder / "2026-10-31 - Dems.md", podnote.note_path(root))   # the linked Apple show's folder

    def test_cache_cleanup_covers_youtube_jobs(self):
        old = Path(podnote.CACHE) / "jobs" / f"youtube-{VIDEO}"
        (old / "asr").mkdir(parents=True)
        (old / "asr" / "transcript-v1.json").write_text("{}")
        import os
        os.utime(old / "asr" / "transcript-v1.json", (0, 0))
        with mock.patch.object(podqueue, "CACHE", Path(podnote.CACHE)):
            self.assertIn("removed 1 episode folders", podqueue.clean_cache([]))
        self.assertFalse(old.exists())


class YouTubeMonitorTests(TwinFixture):

    def setUp(self):
        super().setUp()
        for name, value in (("STATE", self.state), ("SOURCE_WAIT_HOURS", 0)):   # the wait is tested on its own
            patcher = mock.patch.object(podmonitor, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.listing = [{"id": "aaaaaaaaaaa", "title": "Old", "seconds": 3600, "signed_in": False}]
        self.infos: dict[str, object] = {}

    def check(self) -> dict:
        def info(video, fresh=False):
            found = self.infos[video]
            if isinstance(found, Exception):
                raise found
            return found
        with mock.patch.object(youtube, "recent_videos", lambda *a, **k: list(self.listing)), \
                mock.patch.object(youtube, "video_info", info):
            podmonitor.check_youtube("1999999901", state := {})
            self.listing = [*self.extra, *self.listing] if hasattr(self, "extra") else self.listing
            podmonitor.check_youtube("1999999901", state)
        return state[f"youtube:{PLAYLIST}"]

    def test_first_sight_then_new_videos_are_queued_with_their_facts(self):
        self.extra = [{"id": VIDEO, "title": "Dems", "seconds": 6016, "signed_in": False},
                      {"id": "bbbbbbbbbbb", "title": "Trailer", "seconds": 61, "signed_in": False},
                      {"id": "ccccccccccc", "title": "Members", "seconds": 3600, "signed_in": True},
                      {"id": "ddddddddddd", "title": "Premiere", "seconds": 0, "signed_in": False}]
        self.infos = {VIDEO: INFO, "ddddddddddd": sources.SourceError("premiere has not started yet; try again later", retry=True)}
        entry = self.check()
        self.assertEqual([f"https://www.youtube.com/watch?v={VIDEO}"], podqueue.read_lines(self.state / "queue.txt"))
        peek = json.loads((self.state / "queue-state.json").read_text())[f"https://www.youtube.com/watch?v={VIDEO}"]["peek"]
        self.assertEqual({"show": "1999999901", "title": INFO["title"], "published": "2026-10-31T19:00:08"},
                         {k: peek[k] for k in ("show", "title", "published")})
        self.assertIn("Sam Rivers joins us.", peek["description"])
        self.assertIn("ddddddddddd", entry["pending"])                         # waits for the premiere
        self.assertEqual({"aaaaaaaaaaa", VIDEO, "bbbbbbbbbbb", "ccccccccccc"}, set(entry["seen"]))

    def test_an_episode_already_queued_from_apple_is_not_queued_again(self):
        podqueue.add(APPLE, "monitor", peek={"show": "1999999901", "title": INFO["title"], "published": "2026-10-31T19:00:00"})
        self.extra = [{"id": VIDEO, "title": "Dems", "seconds": 6016, "signed_in": False}]
        self.infos = {VIDEO: INFO}
        entry = self.check()
        self.assertEqual([APPLE], podqueue.read_lines(self.state / "queue.txt"))       # not queued again
        self.assertIn(VIDEO, entry["held"])                                    # but kept in hand until Apple's is made


    def test_a_tab_read_for_the_first_time_is_its_back_catalogue(self):
        with podshows.editing() as registry:
            podshows.set_youtube(registry["1999999901"], "1999999901", {"channel": CHANNEL, "playlist": "", "linked_by": "user"})
        state = {f"youtube:{CHANNEL}": {"seen": ["aaaaaaaaaaa"], "pending": {}, "initialised": "2026-11-02T13:07:38Z"}}
        old_streams = [{"id": f"Xx0Strea{n:03d}", "title": "Old stream", "seconds": 5400, "signed_in": False, "tab": "streams"}
                       for n in range(3)]
        videos = [{"id": "aaaaaaaaaaa", "title": "Old", "seconds": 3600, "signed_in": False, "tab": "videos"}]
        with mock.patch.object(youtube, "recent_videos", lambda *a, **k: videos + old_streams), \
                mock.patch.object(youtube, "video_info", lambda video, fresh=False: INFO):
            podmonitor.check_youtube("1999999901", state)
        entry = state[f"youtube:{CHANNEL}"]
        self.assertEqual(["streams", "videos"], entry["tabs"])
        self.assertEqual([], podqueue.read_lines(self.state / "queue.txt"))            # nothing old is queued
        new = {"id": VIDEO, "title": "New stream", "seconds": 6016, "signed_in": False, "tab": "streams"}
        with mock.patch.object(youtube, "recent_videos", lambda *a, **k: videos + [new] + old_streams), \
                mock.patch.object(youtube, "video_info", lambda video, fresh=False: INFO):
            podmonitor.check_youtube("1999999901", state)
        self.assertEqual([f"https://www.youtube.com/watch?v={VIDEO}"], podqueue.read_lines(self.state / "queue.txt"))


    def test_a_fresh_stream_on_a_tab_seen_late_is_queued(self):
        with podshows.editing() as registry:
            podshows.set_youtube(registry["1999999901"], "1999999901", {"channel": CHANNEL, "playlist": "", "linked_by": "user"})
        state = {f"youtube:{CHANNEL}": {"seen": ["aaaaaaaaaaa"], "pending": {}, "tabs": ["videos"],
                                        "initialised": "2026-10-30T12:00:00Z"}}     # Streams unreadable back then
        videos = [{"id": "aaaaaaaaaaa", "title": "Old", "seconds": 3600, "signed_in": False, "tab": "videos"}]
        old = {"id": "Xx0Strea000", "title": "Old stream", "seconds": 5400, "signed_in": False, "tab": "streams"}
        new = {"id": VIDEO, "title": "New stream", "seconds": 6016, "signed_in": False, "tab": "streams"}
        infos = {VIDEO: INFO, old["id"]: {**INFO, "id": old["id"], "timestamp": INFO["timestamp"] - 7 * 86400}}
        with mock.patch.object(youtube, "recent_videos", lambda *a, **k: videos + [new, old]), \
                mock.patch.object(youtube, "video_info", lambda video, fresh=False: infos[video]):
            podmonitor.check_youtube("1999999901", state)
        self.assertEqual([f"https://www.youtube.com/watch?v={VIDEO}"], podqueue.read_lines(self.state / "queue.txt"))
        self.assertIn(old["id"], state[f"youtube:{CHANNEL}"]["seen"])

    def test_an_empty_listing_at_first_sight_initialises_nothing(self):
        self.listing, state = [], {}
        with mock.patch.object(youtube, "recent_videos", lambda *a, **k: []):
            podmonitor.check_youtube("1999999901", state)
        self.assertNotIn("initialised", state[f"youtube:{PLAYLIST}"])

    def test_a_youtube_copy_waits_for_apple_then_is_made_if_apple_has_none(self):
        self.listing = [{"id": "aaaaaaaaaaa", "title": "Old", "seconds": 3600, "signed_in": False}]
        new = {"id": VIDEO, "title": "Dems", "seconds": 6016, "signed_in": False}
        self.infos = {VIDEO: INFO}
        state = {}

        def look(listing):
            with mock.patch.object(youtube, "recent_videos", lambda *a, **k: list(listing)), \
                    mock.patch.object(youtube, "video_info", lambda video, fresh=False: self.infos[video]):
                podmonitor.check_youtube("1999999901", state)
        with mock.patch.object(podmonitor, "SOURCE_WAIT_HOURS", 12):
            look(self.listing)                                                        # first sight
            look([new, *self.listing])
            self.assertEqual([], podqueue.read_lines(self.state / "queue.txt"))       # waiting for Apple
            self.assertIn(VIDEO, state[f"youtube:{PLAYLIST}"]["held"])
            entry = state[f"youtube:{PLAYLIST}"]
            entry["held"][VIDEO]["since"] = "2026-01-01T00:00:00Z"                    # 12 hours have passed
            look([new, *self.listing])
        self.assertEqual([f"https://www.youtube.com/watch?v={VIDEO}"], podqueue.read_lines(self.state / "queue.txt"))
        self.assertNotIn(VIDEO, state[f"youtube:{PLAYLIST}"]["held"])

    def test_a_youtube_copy_is_dropped_when_apple_s_turns_up_while_it_waits(self):
        state = {}
        new = {"id": VIDEO, "title": "Dems", "seconds": 6016, "signed_in": False}
        with mock.patch.object(podmonitor, "SOURCE_WAIT_HOURS", 12), \
                mock.patch.object(youtube, "video_info", lambda video, fresh=False: INFO):
            with mock.patch.object(youtube, "recent_videos", lambda *a, **k: list(self.listing)):
                podmonitor.check_youtube("1999999901", state)
            with mock.patch.object(youtube, "recent_videos", lambda *a, **k: [new, *self.listing]):
                podmonitor.check_youtube("1999999901", state)
                podqueue.add(APPLE, "monitor", peek=self.apple)                       # Apple's copy arrives
                podmonitor.check_youtube("1999999901", state)
        self.assertEqual([APPLE], podqueue.read_lines(self.state / "queue.txt"))
        self.assertIn(VIDEO, state[f"youtube:{PLAYLIST}"]["held"])            # still in hand: Apple's may yet fail
        queue = podqueue.read_lines(self.state / "queue.txt")
        podqueue.finish(APPLE, queue, podqueue.load_state())                  # it did fail, for good
        state[f"youtube:{PLAYLIST}"]["held"][VIDEO]["since"] = "2026-01-01T00:00:00Z"
        with mock.patch.object(podmonitor, "SOURCE_WAIT_HOURS", 12), \
                mock.patch.object(youtube, "video_info", lambda video, fresh=False: INFO), \
                mock.patch.object(youtube, "recent_videos", lambda *a, **k: [new, *self.listing]):
            podmonitor.check_youtube("1999999901", state)
        self.assertEqual([f"https://www.youtube.com/watch?v={VIDEO}"], podqueue.read_lines(self.state / "queue.txt"))


    def test_a_held_video_is_not_looked_up_again(self):
        new = {"id": VIDEO, "title": "Dems", "seconds": 6016, "signed_in": False}
        lookups = []
        state = {}
        with mock.patch.object(podmonitor, "SOURCE_WAIT_HOURS", 12), \
                mock.patch.object(youtube, "video_info", lambda video, fresh=False: lookups.append(video) or INFO):
            for listing in (self.listing, [new, *self.listing], [new, *self.listing], [new, *self.listing]):
                with mock.patch.object(youtube, "recent_videos", lambda *a, l=listing, **k: list(l)):
                    podmonitor.check_youtube("1999999901", state)
        self.assertEqual([VIDEO], lookups)                                     # once, then from what was kept
        self.assertEqual(INFO["title"], state[f"youtube:{PLAYLIST}"]["held"][VIDEO]["peek"]["title"])


    def test_a_waiting_video_pushed_out_of_the_listing_is_still_made(self):
        new = {"id": VIDEO, "title": "Dems", "seconds": 6016, "signed_in": False}
        newer = [{"id": f"Xx0Newer{n:03d}", "title": "Clip", "seconds": 60, "signed_in": False} for n in range(15)]
        state = {}
        with mock.patch.object(podmonitor, "SOURCE_WAIT_HOURS", 12), \
                mock.patch.object(youtube, "video_info", lambda video, fresh=False: INFO):
            for listing in (self.listing, [new, *self.listing]):
                with mock.patch.object(youtube, "recent_videos", lambda *a, l=listing, **k: list(l)):
                    podmonitor.check_youtube("1999999901", state)
            state[f"youtube:{PLAYLIST}"]["held"][VIDEO]["since"] = "2026-01-01T00:00:00Z"    # the wait is over
            with mock.patch.object(youtube, "recent_videos", lambda *a, **k: list(newer)):     # 15 newer: it is gone
                podmonitor.check_youtube("1999999901", state)
        self.assertEqual([f"https://www.youtube.com/watch?v={VIDEO}"], podqueue.read_lines(self.state / "queue.txt"))

    def test_a_pending_video_pushed_out_of_the_listing_is_still_looked_up(self):
        premiere = {"id": VIDEO, "title": "Premiere", "seconds": 0, "signed_in": False}
        self.infos = {VIDEO: sources.SourceError("premiere has not started yet; try again later", retry=True)}
        self.extra = [premiere]
        entry = self.check()
        self.assertIn(VIDEO, entry["pending"])
        self.infos = {VIDEO: INFO}                                             # it has premiered, off the listing
        state = {f"youtube:{PLAYLIST}": entry}
        with mock.patch.object(youtube, "recent_videos", lambda *a, **k: list(self.listing)), \
                mock.patch.object(youtube, "video_info", lambda video, fresh=False: self.infos[video]):
            podmonitor.check_youtube("1999999901", state)
        self.assertEqual([f"https://www.youtube.com/watch?v={VIDEO}"], podqueue.read_lines(self.state / "queue.txt"))
        self.assertNotIn(VIDEO, entry["pending"])

    def test_a_video_released_after_the_follow_is_not_back_catalogue(self):
        with podshows.editing() as registry:
            registry["1999999901"]["monitor_since"] = "2026-10-31T18:00:00Z"
        older = {**INFO, "id": "aaaaaaaaaaa", "timestamp": INFO["timestamp"] - 86400}
        infos = {VIDEO: INFO, "aaaaaaaaaaa": older}
        listing = [{"id": VIDEO, "title": "Dems", "seconds": 6016, "signed_in": False}, *self.listing]
        state = {}
        with mock.patch.object(youtube, "recent_videos", lambda *a, **k: list(listing)), \
                mock.patch.object(youtube, "video_info", lambda video, fresh=False: infos[video]):
            podmonitor.check_youtube("1999999901", state)                      # the first read after the follow
        self.assertEqual([f"https://www.youtube.com/watch?v={VIDEO}"], podqueue.read_lines(self.state / "queue.txt"))
        self.assertIn("aaaaaaaaaaa", state[f"youtube:{PLAYLIST}"]["seen"])     # from before the follow: not queued

    def test_a_first_read_youtube_cannot_describe_yet_is_tried_again(self):
        with podshows.editing() as registry:
            registry["1999999901"]["monitor_since"] = "2026-10-31T18:00:00Z"
        listing = [{"id": VIDEO, "title": "Dems", "seconds": 6016, "signed_in": False}, *self.listing]
        state, infos = {}, {VIDEO: sources.SourceError("bot check; try again later", retry=True)}
        def info(video, fresh=False):
            found = infos.get(video, {**INFO, "id": video, "timestamp": INFO["timestamp"] - 86400})
            if isinstance(found, Exception):
                raise found
            return found
        with mock.patch.object(youtube, "recent_videos", lambda *a, **k: list(listing)), \
                mock.patch.object(youtube, "video_info", info):
            podmonitor.check_youtube("1999999901", state)
            self.assertNotIn("initialised", state[f"youtube:{PLAYLIST}"])           # nothing marked seen unproven
            infos[VIDEO] = {**INFO, "timestamp": None, "upload_date": "20261031"}   # a day only: the follow's own
            podmonitor.check_youtube("1999999901", state)
        self.assertEqual([f"https://www.youtube.com/watch?v={VIDEO}"], podqueue.read_lines(self.state / "queue.txt"))

    def test_an_episode_waiting_for_its_language_survives_leaving_the_listing(self):
        new = {"id": VIDEO, "title": "Dems", "seconds": 6016, "signed_in": False}
        newer = [{"id": f"Xx0Newer{n:03d}", "title": "Clip", "seconds": 60, "signed_in": False} for n in range(15)]
        state, answers = {}, ["Not queued yet: this episode looks Greek (from its title). Which language is it in?"]
        offered = []
        def add(link, source="", peek=None, **kw):
            offered.append(link)
            return answers[0]
        with mock.patch.object(youtube, "video_info", lambda video, fresh=False: INFO), \
                mock.patch.object(podqueue, "add", add):
            for listing in (self.listing, [new, *self.listing]):
                with mock.patch.object(youtube, "recent_videos", lambda *a, l=listing, **k: list(l)):
                    podmonitor.check_youtube("1999999901", state)
            self.assertIn(VIDEO, state[f"youtube:{PLAYLIST}"]["held"])
            answers[0] = "Queued (position 1 of 1). Language: Greek."                # answered meanwhile
            with mock.patch.object(youtube, "recent_videos", lambda *a, **k: list(newer)):
                podmonitor.check_youtube("1999999901", state)
        self.assertEqual(f"https://www.youtube.com/watch?v={VIDEO}", offered[-1])
        self.assertNotIn(VIDEO, state[f"youtube:{PLAYLIST}"]["held"])

    def test_a_video_refused_for_a_week_is_reported_once_and_kept(self):
        self.extra = [{"id": VIDEO, "title": "Dems", "seconds": 6016, "signed_in": False}]
        self.infos = {VIDEO: sources.SourceError("bot check; try again later", retry=True)}
        entry = self.check()
        entry["pending"][VIDEO] = "2026-01-01T00:00:00Z"                              # long ago
        state, messages = {f"youtube:{PLAYLIST}": entry}, []
        with mock.patch.object(youtube, "recent_videos", lambda *a, **k: list(self.listing)), \
                mock.patch.object(youtube, "video_info", lambda video, fresh=False: (_ for _ in ()).throw(self.infos[video])):
            podmonitor.check_youtube("1999999901", state, messages)
            podmonitor.check_youtube("1999999901", state, messages)
        self.assertEqual(1, len(messages))
        self.assertIn(VIDEO, entry["pending"])

class RegistryLinkTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(podshows, "STATE", Path(directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.registry = {"1999999901": {"title": "Harbour Talk", "folder": "Harbour Talk", "country": "gb", "followers": ["admin"],
                                        "hosts": [], "fixes": {}, "advertisers": {}}}

    def test_wikidata_links_a_show_to_its_channel_and_playlist(self):
        rows = {"results": {"bindings": [{"channel": {"value": CHANNEL}}]}}
        found = {"channel": CHANNEL, "title": "Harbour Talk", "playlist": PLAYLIST, "handle": "@harbourtalk"}
        with mock.patch("web.http_json", return_value=rows), mock.patch.object(youtube, "channel", return_value=found):
            line = podshows.link_from_wikidata(self.registry, "1999999901")
        self.assertEqual("Harbour Talk: linked to YouTube Harbour Talk (podcast playlist).", line)
        linked = podshows.youtube_link(self.registry["1999999901"], "1999999901")
        self.assertEqual((CHANNEL, PLAYLIST, "Wikidata", 2), tuple(linked[k] for k in ("channel", "playlist", "linked_by", "priority")))
        self.assertEqual(["apple", "youtube"], [s["type"] for s in podshows.sources_of("1999999901", self.registry["1999999901"])])
        self.assertEqual("1999999901", podshows.find(self.registry, "https://www.youtube.com/@harbourtalk"))
        self.assertEqual("1999999901", podshows.find(self.registry, f"https://www.youtube.com/playlist?list={PLAYLIST}"))
        with mock.patch("web.http_json") as again:
            self.assertEqual("", podshows.link_from_wikidata(self.registry, "1999999901"))   # once per show
        again.assert_not_called()

    def test_no_channel_on_wikidata_is_remembered(self):
        with mock.patch("web.http_json", return_value={"results": {"bindings": []}}):
            self.assertEqual("", podshows.link_from_wikidata(self.registry, "1999999901"))
        self.assertIn("youtube_checked", self.registry["1999999901"])
        self.assertNotIn("youtube", self.registry["1999999901"])


class AppleLookupErrorTests(unittest.TestCase):

    def test_only_a_missing_episode_or_a_server_error_is_tried_again(self):
        from sources import apple
        for text, retry, says in (("Apple episode 1 was not returned for show 2; the episode may be newly indexed", True, "not indexed"),
                                  ("Apple episode 1 ('x') did not match the RSS feed", True, "not indexed"),
                                  ("Feed returned HTTP 503", True, "try again later"),
                                  ("Feed returned HTTP 410", False, "HTTP 410"),
                                  ("Show 2 has no public RSS feed", False, "no public RSS feed")):
            error = apple.lookup_error(LookupError(text))
            self.assertEqual(retry, error.retry, text)
            self.assertIn(says, str(error), text)
        self.assertNotIn("not indexed", str(apple.lookup_error(LookupError("Feed returned HTTP 410"))))


class SourcesTests(unittest.TestCase):

    def test_every_video_of_a_linked_channel_is_the_show_s(self):
        registry = {"1999999901": {"title": "Harbour Talk", "sources": [{"type": "apple", "id": "1999999901", "country": "gb", "priority": 1}, {"type": "youtube", "channel": CHANNEL, "playlist": PLAYLIST, "priority": 2}]}}
        with mock.patch.object(podshows, "load", return_value=registry):
            self.assertEqual("1999999901", youtube.show_key(CHANNEL))                      # any video of the channel
            self.assertEqual("yt:UCanotherChannel00000000Cc", youtube.show_key("UCanotherChannel00000000Cc"))

    def test_a_show_has_many_sources_in_the_order_the_admin_sets(self):
        show = {"title": "Harbour Talk", "folder": "Harbour Talk", "country": "gb"}
        self.assertEqual([("apple", 1)], [(s["type"], s["priority"]) for s in podshows.sources_of("1999999901", show)])  # its key's
        podshows.set_youtube(show, "1999999901", {"channel": CHANNEL, "playlist": PLAYLIST, "linked_by": "user"})
        self.assertEqual([("apple", 1), ("youtube", 2)], [(s["type"], s["priority"]) for s in podshows.sources_of("1999999901", show)])
        self.assertTrue(podshows.outranked("1999999901", show, "youtube"))
        self.assertEqual(["youtube", "apple"], podshows.set_priority(show, "1999999901", ["youtube"]))
        self.assertTrue(podshows.outranked("1999999901", show, "apple"))
        self.assertFalse(podshows.outranked("1999999901", show, "youtube"))
        podshows.set_youtube(show, "1999999901", None)
        self.assertEqual(["apple"], [s["type"] for s in podshows.sources_of("1999999901", show)])
        with self.assertRaisesRegex(SystemExit, "has no youtube source"):
            podshows.set_priority(show, "1999999901", ["youtube"])
        self.assertEqual([("youtube", 1)], [(s["type"], s["priority"]) for s in podshows.sources_of(f"yt:{CHANNEL}", {})])


class ChannelTabTests(unittest.TestCase):

    def test_a_channel_is_listed_from_its_videos_and_its_streams(self):
        def ytdlp(arguments, timeout=0):
            url = arguments[-1]
            if url.endswith("/streams"):
                if "UCnostreams" in url:
                    raise sources.SourceError("yt-dlp failed: This channel does not have a streams tab")
                return {"entries": [{"id": "Xx0Stream01", "title": "Live episode", "duration": 5400},
                                    {"id": "Xx0Test0Ep1", "title": "Also a video", "duration": 3600}]}
            return {"entries": [{"id": "Xx0Test0Ep1", "title": "A video", "duration": 3600}]}
        with mock.patch.object(youtube, "ytdlp", ytdlp):
            listed = youtube.recent_videos(CHANNEL)
            self.assertEqual([("Xx0Test0Ep1", "videos"), ("Xx0Stream01", "streams")], [(v["id"], v["tab"]) for v in listed])
            self.assertEqual(["videos"], [v["tab"] for v in youtube.recent_videos("UCnostreams000000000000")])
            self.assertEqual(["playlist"], [v["tab"] for v in youtube.recent_videos(playlist=PLAYLIST)])
            self.assertEqual([("Xx0Test0Ep1", "playlist"), ("Xx0Stream01", "streams")],          # all three, each video once
                             [(v["id"], v["tab"]) for v in youtube.recent_videos(CHANNEL, PLAYLIST)])


class FollowAgainTests(TwinFixture):

    def test_a_show_followed_again_starts_afresh(self):
        feed = [SimpleNamespace(guid="g-old", title="Old", published=datetime(2026, 10, 1, tzinfo=timezone.utc),
                                duration="3600", description="", enclosure_url="https://a.example/1.mp3")]
        state = {"1999999901": {"seen": [], "pending": {}, "initialised": "2026-09-01T00:00:00Z"}}
        with podshows.editing() as registry:
            registry["1999999901"]["monitor_since"] = "2026-11-02T12:00:00Z"   # unfollowed, then followed again today
        with mock.patch.object(podmonitor, "STATE", self.state), mock.patch.object(podmonitor, "feed_episodes", return_value=feed), \
                mock.patch.object(podmonitor, "apple_episodes", return_value=[]):
            podmonitor.check_apple("1999999901", "gb", state, [])
        self.assertEqual(["g-old"], state["1999999901"]["seen"])               # the gap is back catalogue, not queued
        self.assertEqual([], podqueue.read_lines(self.state / "queue.txt"))

    def test_an_apple_copy_waits_for_youtube_when_youtube_comes_first(self):
        released = datetime(2026, 10, 31, 19, 0, tzinfo=timezone.utc)
        feed = [SimpleNamespace(guid="g-new", title=self.apple["title"], published=released, duration="3600",
                                description="", enclosure_url="https://a.example/2.mp3")]
        state = {"1999999901": {"seen": [], "pending": {}, "initialised": "2026-10-01T00:00:00Z"}}
        with podshows.editing() as registry:
            podshows.set_priority(registry["1999999901"], "1999999901", ["youtube", "apple"])
        video = f"https://www.youtube.com/watch?v={VIDEO}"
        podqueue.add(video, "monitor", peek=self.video)                       # YouTube's copy is on its way

        def look():
            with mock.patch.object(podmonitor, "STATE", self.state), mock.patch.object(podmonitor, "SOURCE_WAIT_HOURS", 12), \
                    mock.patch.object(podmonitor, "feed_episodes", return_value=feed), \
                    mock.patch.object(podmonitor, "apple_episodes", return_value=[]), \
                    mock.patch.object(podmonitor, "matched_links", return_value={"g-new": APPLE}):
                podmonitor.check_apple("1999999901", "gb", state, [])
        look()
        self.assertEqual([video], podqueue.read_lines(self.state / "queue.txt"))
        self.assertIn("g-new", state["1999999901"]["held"])                  # kept in hand while YouTube's may fail
        podqueue.finish(video, podqueue.read_lines(self.state / "queue.txt"), podqueue.load_state())   # it failed
        state["1999999901"]["held"]["g-new"] = "2026-01-01T00:00:00Z"        # 12 hours have passed
        look()
        self.assertEqual([APPLE], podqueue.read_lines(self.state / "queue.txt"))
        self.assertNotIn("g-new", state["1999999901"]["held"])

    def test_a_pending_episode_is_cleared_once_another_source_has_made_it(self):
        released = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
        feed = [SimpleNamespace(guid="g-gone", title="Five Murders", published=released, duration="2517",
                                description="", enclosure_url="https://a.example/5.mp3")]
        state = {"1999999901": {"seen": [], "pending": {}, "initialised": "2026-10-01T00:00:00Z"}}
        video = f"https://www.youtube.com/watch?v={VIDEO}"
        podqueue.add(video, "monitor", peek={"show": "1999999901", "title": "They're Lying To You",
                                             "published": "2026-10-29T15:00:11", "seconds": 2390})

        def look(messages):
            with mock.patch.object(podmonitor, "STATE", self.state), \
                    mock.patch.object(podmonitor, "feed_episodes", return_value=feed), \
                    mock.patch.object(podmonitor, "apple_episodes", return_value=[]):        # Apple never lists it
                podmonitor.check_apple("1999999901", "gb", state, messages)
        look([])
        self.assertIn("g-gone", state["1999999901"]["pending"])               # only queued: it may still fail
        (self.folder / "2026-10-29 - They're Lying To You.md").write_text(    # made from YouTube
            '---\nepisode: "They\'re Lying To You"\npublished: "2026-10-29T15:00:11"\nduration: 2390\n'
            f"youtube_video_id: {VIDEO}\n---\n# They're Lying To You\n", encoding="utf-8")
        messages = []
        with mock.patch.object(podqueue, "now", lambda: podqueue.parse_iso("2026-11-05T00:00:00Z")):   # past the notice
            look(messages)
        self.assertEqual([], messages)                                       # no "still unresolved"
        self.assertNotIn("g-gone", state["1999999901"]["pending"])
        self.assertIn("g-gone", state["1999999901"]["seen"])

    def test_a_channel_already_linked_is_never_entered_twice(self):
        with podshows.editing() as registry:
            podshows.set_youtube(registry["1999999901"], "1999999901",
                                 {"channel": CHANNEL, "playlist": PLAYLIST, "handle": "@HarbourTalk", "linked_by": "user"})
            self.assertEqual("1999999901", podshows.find(registry, "https://www.youtube.com/@harbourtalk"))   # any case
            with mock.patch.object(youtube, "channel", return_value={"channel": CHANNEL, "title": "Harbour Talk", "playlist": ""}):
                self.assertEqual("1999999901", podshows.register_youtube(registry, f"https://www.youtube.com/watch?v={VIDEO}"))
        self.assertNotIn(f"yt:{CHANNEL}", podshows.load())

    def test_podnote_keeps_the_language_the_queue_announced(self):
        record = {"show": "Harbour Talk", "episode": "Η νύχτα στο λιμάνι και όλα όσα έρχονται τώρα",
                  "episode_description": "Μια συζήτηση για το λιμάνι και την πόλη μας απόψε.", "language_hint": "el"}
        with mock.patch.object(podshows, "load", return_value={}):
            self.assertEqual(("en", "default"), podnote.episode_language(record, "en", "default"))   # as the user was told
            self.assertEqual(("el", "detected"), podnote.episode_language(record))                  # by hand: it decides
