#!/usr/bin/env python3
"""Unit tests for episode languages: the guess, the question before queueing, the show's confirmed
language, and the language given to the speech model, the note prompts, the note and the validation pass."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import languages
import podmonitor
import podnote
import podqueue
import podshows
import podverify
import sources
from sources import youtube

VIDEO = "https://www.youtube.com/watch?v=Xx0Test0Ep1"
CHANNEL = "UCtestChannel000000000Aa"
PEEK = {"show": f"yt:{CHANNEL}", "title": "Το λιμάνι αλλάζει: τι σημαίνει για την πόλη. 01.11.2026",
           "published": "2026-11-01T18:46:09", "description": "Η Ελένη Μάρκου μιλά για το λιμάνι και για όσα έρχονται.",
           "language_hint": "el"}


class GuessTests(unittest.TestCase):

    def test_names_and_codes(self):
        self.assertEqual(["el", "el", "el", "el", "en", "en", "de", None, None],
                         [languages.code(x) for x in ("Greek", "el", "el-GR", "ελληνικά", "English", "en-US", "Deutsch", "no", "yes")])
        self.assertEqual("Greek", languages.name("el"))

    def test_scripts_and_common_words(self):
        self.assertEqual("el", languages.guess(PEEK["title"]))
        self.assertEqual("uk", languages.guess("Це подкаст про кулінарію, і ми готуємо їжу з різних країн світу"))
        self.assertEqual("de", languages.guess("In dieser Folge sprechen wir über die Wirtschaft und die Politik, wie es weitergeht"))
        self.assertEqual("en", languages.guess("Sam Rivers joins us to explain why the council should rebuild the harbour in November"))
        self.assertEqual("en", languages.guess("Anna Smith and John Brown talk to Ελένη about the harbour and what comes next for the town"))
        self.assertIsNone(languages.guess("Ep. 12"))

    def test_confident_only_when_the_evidence_agrees(self):
        title, description = PEEK["title"], PEEK["description"]
        self.assertEqual(("el", True), languages.judge("el", title, description)[:2])          # tag and text agree
        self.assertEqual(("el", True), languages.judge("", title, description)[:2])            # title and description each
        self.assertEqual(("el", False), languages.judge("", title, "")[:2])                    # the title alone
        self.assertEqual(("el", False), languages.judge("el", "", "")[:2])                     # the tag alone
        conflict = languages.judge("en", title, description)                                   # a wrong default tag
        self.assertEqual(("el", False), conflict[:2])
        self.assertIn("tags it English but its title and description look Greek", conflict[2])
        self.assertEqual(("en", True), languages.judge("en-us", "How the war changes Europe, with the editors",
                                                       "We talk about the front and what it means for all of us.")[:2])

    def test_one_order_decides_every_episode_s_language(self):
        title, description = PEEK["title"], PEEK["description"]
        decide = languages.decide
        self.assertEqual(("de", "user"), decide("de", "el", ("en", "detected"), "el", title, description)[:2])  # the user's
        self.assertEqual(("el", "show"), decide(None, "el", ("en", "detected"))[:2])                       # then the show's
        self.assertEqual(("en", "detected"), decide(None, "", ("en", "detected"), "el", title, description)[:2])  # earlier
        self.assertEqual(("el", "detected"), decide(None, "", None, "el", title, description)[:2])          # then evidence
        self.assertEqual((None, "ask", "el"), decide(None, "", None, "", title, "")[:3])                    # unsure: ask
        self.assertEqual(("en", "default"), decide(None)[:2])                                                # else English

    def test_an_answer_that_slipped_into_english(self):
        english = ("**Topic:** The closing of the local paper and the effort to save its archive.\n"
                   "- The host says that the paper was closed because it would not stop reporting on the council.\n"
                   "**References:**\n- none")
        native = ("**Topic:** Συζήτηση για το κλείσιμο της εφημερίδας και το αρχείο του Mr. Smith.\n"
                  "- Ο παρουσιαστής λέει ότι η εφημερίδα έκλεισε επειδή έγραφε για το δημοτικό συμβούλιο.\n"
                   "**References:**\n- [work] The Brothers Karamazov (Dostoevsky) — cited by the host as an example of the novel.")
        self.assertEqual("en", languages.drifted(english, "el"))
        self.assertIsNone(languages.drifted(native, "el"))
        self.assertIsNone(languages.drifted("SKIP", "el"))


class QueueLanguageTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.state, notes = root / "state", root / "notes"
        (notes / "Podcasts").mkdir(parents=True)
        for module, name, value in ((podqueue, "STATE", self.state), (podqueue, "ROOT", notes), (podqueue, "EPISODES", Path("Podcasts")),
                                    (podshows, "STATE", self.state), (podmonitor, "STATE", self.state)):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state.mkdir()
        self.registry({f"yt:{CHANNEL}": {"title": "Ελένη Μάρκου", "folder": "Ελένη Μάρκου", "country": "gb", "followers": ["admin"],
                                         "hosts": [], "fixes": {}, "advertisers": {}}})

    def registry(self, shows: dict) -> None:
        (self.state / "shows.json").write_text(json.dumps(shows, ensure_ascii=False), encoding="utf-8")

    def queue(self) -> list[str]:
        return podqueue.read_lines(self.state / "queue.txt")

    def add(self, **kwargs) -> str:
        with mock.patch.object(youtube, "peek", return_value=dict(PEEK)):
            return podqueue.add(VIDEO, "chat", **kwargs)

    def test_a_clearly_non_english_link_is_queued_as_detected(self):
        self.assertEqual("Queued (position 1 of 1). Language: Greek.", self.add())
        entry = json.loads((self.state / "queue-state.json").read_text())[VIDEO]
        self.assertEqual(("el", "detected"), (entry["language"], entry["language_source"]))
        self.assertEqual("", podshows.language_of(f"yt:{CHANNEL}"))      # the episode's, not the show's
        with mock.patch.object(podqueue, "run_owned", return_value=(1, "STOPPED: x")) as runner, \
                mock.patch.object(podqueue, "run_verify"):
            podqueue.run()
        self.assertEqual(["--language", "el", "--language-source", "detected"], runner.call_args.args[0][-4:])

    def test_a_link_that_may_be_non_english_is_asked_about_not_queued(self):
        with mock.patch.object(youtube, "peek", return_value=PEEK | {"description": "", "language_hint": ""}):
            said = podqueue.add(VIDEO, "chat")
        self.assertEqual('Not queued yet: this episode looks Greek (from its title and description). Which language is it '
                         'in? Reply with it, for example "Greek" or "English".', said)
        self.assertEqual([], self.queue())
        self.assertEqual("It looks Greek, and its language was not confirmed: send the link again and say which language "
                         "it is in.", podqueue.plain_reason(said))

    def test_the_answer_queues_it_and_becomes_the_show_s_language(self):
        self.assertEqual("Queued (position 1 of 1). Language: Greek.", self.add(language="Greek"))
        entry = json.loads((self.state / "queue-state.json").read_text())[VIDEO]
        self.assertEqual(("el", "user"), (entry["language"], entry["language_source"]))
        self.assertEqual("el", podshows.language_of(f"yt:{CHANNEL}"))
        with mock.patch.object(podqueue, "run_podnote", return_value=(1, "STOPPED: x")) as runner, \
                mock.patch.object(podqueue, "run_verify"):
            podqueue.run()
        self.assertEqual("el", runner.call_args.args[1])           # podnote is told the language given
        self.assertEqual([], self.queue())                          # that run failed: out of the queue
        self.assertEqual("Queued (position 1 of 1). Language: Greek.", self.add())

    def test_an_unknown_language_is_refused(self):
        self.assertIn("is not a language I know", self.add(language="Klingonish"))

    def test_an_english_episode_is_queued_and_the_language_said(self):
        with mock.patch.object(youtube, "peek", return_value=PEEK | {"title": "How the war changes Europe, with the editors",
                                                                         "description": "We talk about the front and what it means.",
                                                                         "language_hint": "en-US"}):
            self.assertEqual("Queued (position 1 of 1). Language: English.", podqueue.add(VIDEO, "chat"))

    def test_the_monitor_holds_the_episode_and_asks_once(self):
        info = {"id": "Xx0Test0Ep1", "title": PEEK["title"], "description": PEEK["description"], "language": "en",
                "duration": 1410, "timestamp": 1793534769, "channel_id": CHANNEL}
        listing = [{"id": "Xx0Test0Ep1", "title": PEEK["title"], "seconds": 1410, "signed_in": False}]
        state, messages = {f"youtube:{CHANNEL}": {"seen": [], "pending": {}, "initialised": "2026-11-01T00:00:00Z"}}, []
        with mock.patch.object(youtube, "recent_videos", return_value=listing), mock.patch.object(youtube, "video_info", return_value=info):
            podmonitor.check_youtube(f"yt:{CHANNEL}", state, messages)
            podmonitor.check_youtube(f"yt:{CHANNEL}", state, messages)
            self.assertEqual([], self.queue())
            self.assertEqual(1, len(messages))
            self.assertIn('Ελένη Μάρκου: a new episode looks Greek', messages[0])
            with podshows.editing() as registry:
                podshows.set_language(registry[f"yt:{CHANNEL}"], "el")      # "Ελένη Μάρκου is in Greek"
            podmonitor.check_youtube(f"yt:{CHANNEL}", state, messages)
        self.assertEqual([VIDEO], self.queue())
        self.assertIn("Xx0Test0Ep1", state[f"youtube:{CHANNEL}"]["seen"])


    def test_the_language_question_goes_to_the_admin_even_for_a_show_a_member_added(self):
        with podshows.editing() as registry:
            registry[f"yt:{CHANNEL}"]["added_by"] = "kit"
        info = {"id": "Xx0Test0Ep1", "title": PEEK["title"], "description": PEEK["description"], "language": "en",
                "duration": 1410, "timestamp": 1793534769, "channel_id": CHANNEL}
        listing = [{"id": "Xx0Test0Ep1", "title": PEEK["title"], "seconds": 1410, "signed_in": False}]
        state, messages = {f"youtube:{CHANNEL}": {"seen": [], "pending": {}, "initialised": "2026-11-01T00:00:00Z"}}, []
        with mock.patch.object(youtube, "recent_videos", return_value=listing), \
                mock.patch.object(youtube, "video_info", return_value=info), mock.patch("notify.to_user") as member:
            podmonitor.check_youtube(f"yt:{CHANNEL}", state, messages)
        self.assertEqual(1, len(messages))                                   # the admin's, as monitor output
        member.assert_not_called()
        self.assertEqual(["Xx0Test0Ep1"], state[f"youtube:{CHANNEL}"]["language_asked"])

class PodnoteLanguageTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / f"youtube-Xx0Test0Ep1"
        (self.root / "source").mkdir(parents=True)
        (self.root / "asr").mkdir()
        self.record = {"source_url": VIDEO, "youtube_video_id": "Xx0Test0Ep1", "show": "Ελένη Μάρκου",
                       "episode": PEEK["title"], "published": "2026-11-01", "duration_seconds": 1410,
                       "episode_description": PEEK["description"], "youtube_channel_id": CHANNEL,
                       "published_utc": PEEK["published"], "language_hint": "el"}
        patcher = mock.patch.object(podshows, "load", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_unconfirmed_non_english_episode_stops_before_download(self):
        self.assertEqual(("el", "detected"), podnote.episode_language(self.record))      # tag and text agree
        self.assertEqual(("el", "detected"), podnote.episode_language(self.record | {"language_hint": ""}, "el", "detected"))
        with self.assertRaisesRegex(podnote.Stop, "looks Greek .* no language is confirmed"):
            podnote.episode_language(self.record | {"language_hint": "en"})            # tag and text disagree
        self.assertEqual(("el", "user"), podnote.episode_language(self.record, "Greek"))
        self.assertEqual(("el", "user"), podnote.episode_language(self.record | {"language": "el", "language_source": "user"}))
        english = {"episode": "Ep", "episode_description": "A talk about the history of the moon landings and what came after."}
        self.assertEqual(("en", "default"), podnote.episode_language(english))
        with mock.patch.object(podshows, "load", return_value={f"yt:{CHANNEL}": {"language": "el"}}):
            self.assertEqual(("el", "show"), podnote.episode_language(self.record))

    def test_the_prompts_and_the_note_carry_the_language(self):
        (self.root / "source" / "resolved-episode-v1.json").write_text(
            json.dumps(self.record | {"language": "el", "language_source": "user"}, ensure_ascii=False), encoding="utf-8")
        (self.root / "asr" / "transcript-v1.json").write_text(json.dumps({"chunks": [{"text": "Καλησπέρα. Το λιμάνι αλλάζει."}]}),
                                                              encoding="utf-8")
        asked = []

        def ask(base, headers, prompt, limit, system=""):
            asked.append((prompt, limit))
            self.assertTrue(system.startswith("Write your whole answer in Greek") or prompt.startswith("Choose the subject tags"))
            return ("**Participants:** **Ελένη Μάρκου** (host)\n\nΣύντομη περίληψη." if "Participants" in prompt
                    else "- Σημείο.", "stop")
        with mock.patch.object(podnote, "ask", ask), mock.patch.object(podnote, "log"):
            note, problems = podnote.synthesise(self.root, "http://model", {})
        self.assertEqual([], problems)
        content = [(p, limit) for p, limit in asked if not p.startswith("Choose the subject tags")]
        self.assertTrue(all("in Greek" in p for p, _ in content), [p[:200] for p, _ in content])
        self.assertIn(round(podnote.SECTION_MAX_TOKENS * podnote.OTHER_LANGUAGE_TOKENS), [limit for _, limit in content])
        self.assertIn("\nlanguage: el\n", note)

    def test_english_prompts_say_english_too(self):
        record = dict(self.record, episode="Ep", episode_description="A talk about the moon.", language="en",
                      language_source="default", show="Moon Show")
        record.pop("language_hint")
        (self.root / "source" / "resolved-episode-v1.json").write_text(json.dumps(record), encoding="utf-8")
        (self.root / "asr" / "transcript-v1.json").write_text(json.dumps({"chunks": [{"text": "Hello. The moon."}]}), encoding="utf-8")
        asked = []
        with mock.patch.object(podnote, "ask", lambda b, h, p, l, s="": (asked.append((p, l, s)) or ("- A point.", "stop"))), \
                mock.patch.object(podnote, "log"):
            note, _ = podnote.synthesise(self.root, "http://model", {})
        self.assertTrue(all("in English" in p for p, _, _ in asked if not p.startswith("Choose the subject tags")))
        self.assertEqual({""}, {system for _, _, system in asked})               # English prompts are as before
        self.assertFalse(any("Reminder:" in p for p, _, _ in asked))
        self.assertIn(podnote.SECTION_MAX_TOKENS, [limit for _, limit, _ in asked])
        self.assertIn("\nlanguage: en\n", note)


    def test_a_section_that_slipped_into_english_is_asked_again(self):
        (self.root / "source" / "resolved-episode-v1.json").write_text(
            json.dumps(self.record | {"language": "el", "language_source": "user"}, ensure_ascii=False), encoding="utf-8")
        (self.root / "asr" / "transcript-v1.json").write_text(json.dumps({"chunks": [{"text": "Καλησπέρα. Το λιμάνι αλλάζει."}]}),
                                                              encoding="utf-8")
        english = "**Topic:** The harbour is changing.\n- The host says that the harbour is changing and the work goes on for now."
        answers = {"section": [english, "**Topic:** Το λιμάνι αλλάζει.\n- Η παρουσιάστρια λέει ότι το λιμάνι αλλάζει."]}

        def ask(base, headers, prompt, limit, system=""):
            if prompt.startswith("You are taking reading notes"):
                return answers["section"].pop(0), "stop"
            return ("**Participants:** **Ελένη Μάρκου** (host)\n\nΣύντομη περίληψη." if "Participants" in prompt
                    else "- Σημείο.", "stop")
        with mock.patch.object(podnote, "ask", ask), mock.patch.object(podnote, "log"):
            note, problems = podnote.synthesise(self.root, "http://model", {})
        self.assertEqual([], problems)
        self.assertEqual([], answers["section"])                                 # asked twice
        section = podnote.work_dir(self.root) / "section-01.md"
        self.assertIn("Η παρουσιάστρια λέει", section.read_text(encoding="utf-8"))
        section.write_text(english, encoding="utf-8")                            # an English section from an earlier run
        answers["section"] = ["**Topic:** Το λιμάνι αλλάζει.\n- Η παρουσιάστρια λέει ξανά ότι το λιμάνι αλλάζει."]
        with mock.patch.object(podnote, "ask", ask), mock.patch.object(podnote, "log"):
            podnote.synthesise(self.root, "http://model", {})
        self.assertIn("Η παρουσιάστρια λέει ξανά", section.read_text(encoding="utf-8"))
        record = json.loads((self.root / "note-record.json").read_text(encoding="utf-8"))
        self.assertEqual(["section 1: cached in en, asked again"], record["language_slips"])   # the last run's


class VerifyLanguageTests(unittest.TestCase):

    def test_claims_are_written_in_the_note_s_language(self):
        sent = []
        model = podverify.Model()
        model.config = {"base_url": "http://model"}
        with mock.patch.object(podnote, "ask", lambda *a: sent.append(a) or ('{"claims": []}', "stop")), \
                mock.patch("omlx_client.headers", return_value={}):
            model.ask("List the claims.")
            model.language = "Greek"
            model.ask("List the claims.")
        english, greek = sent
        self.assertEqual("Language: the podcast and its notes are in English.\n\nList the claims.", english[2])
        self.assertEqual(4, len(english))                                       # no system message
        self.assertIn("Write every claim, \"who\" and reason in Greek.", greek[2])
        self.assertEqual("Write every text the note will show in Greek.", greek[4])

    def test_the_note_s_wikipedia_first_then_english(self):
        asked = []

        def http_json(url, timeout=15):
            asked.append(url.split("/w/api.php")[0])
            return {"query": {"pages": [{"title": "Μάρκου, Ελένη", "index": 1, "description": "δημοσιογράφος"}]}} if "el." in url \
                else {"query": {"pages": []}}
        with mock.patch.object(podverify, "http_json", http_json), mock.patch.object(podverify, "WIKI_LANGUAGES", ["el", "en"]):
            self.assertEqual(["Μάρκου, Ελένη — δημοσιογράφος"], podverify.wikipedia("Ελένη Μάρκου"))
            self.assertTrue(podverify.wiki_url("Μάρκου, Ελένη").startswith("https://el.wikipedia.org/wiki/"))
        self.assertEqual(["https://el.wikipedia.org"], asked)
        asked.clear()
        with mock.patch.object(podverify, "http_json", lambda url, timeout=15: asked.append(url) or {"query": {"pages": []}}), \
                mock.patch.object(podverify, "WIKI_LANGUAGES", ["el", "en"]):
            self.assertEqual([], podverify.wikipedia("Nobody"))
        self.assertEqual(["el", "en"], [u.split("//")[1].split(".")[0] for u in asked])


class RemakeLanguageTests(unittest.TestCase):

    def test_a_one_off_language_stays_the_episode_s(self):
        record = {"show": "Moon Show", "episode": "Ep", "episode_description": "A talk about the moon.",
                  "language": "el", "language_source": "user-episode"}
        with mock.patch.object(podshows, "load", return_value={}):
            self.assertEqual(("el", "user-episode"), podnote.episode_language(record))
        show = {"title": "Moon Show"}
        with mock.patch.object(podshows, "log_change"):
            podshows.ensure_show({"1": show}, "1", "Moon Show", metadata=record)
        self.assertNotIn("language", show)                                    # never the show's
