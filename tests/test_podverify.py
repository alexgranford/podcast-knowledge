#!/usr/bin/env python3
"""Unit tests for the external validation pass: evidence, guarded verdicts, rendering, claims."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import podverify

REAL_BOOK_RECORD = podverify.book_record          # VerifyTests.setUp replaces it with a stub

NOTE = """---
origin: agent
model: gemma-4-26B-A4B-it-qat-4bit
created: "2026-10-07T10:00:00"
updated: "2026-10-07T10:00:00"
show: "Show"
episode: "Episode"
guests: ["Tobias Marlough"]
source_url: https://podcasts.apple.com/gb/podcast/show/id1?i=1000000000001
apple_episode_id: '1000000000001'
duration: 4200
---
# Episode

**Show** · 2026-10-07 · ⏱ 1h 10m

> [!abstract] Summary
> **Participants:** **Rowan Ellis** (host), **Tobias Marlough** (guest, historian)
>
> While Tobias Marlough defends the government, Rowan Ellis presses him on the budget.

## Key themes

### The budget
> Tobias Marlough and Rowan Ellis clash.

- **Apollo** — Tobias Marlough cites the Apollo programme.

## References

### Works
- The Culture Series (Iain Banks) — a positive vision
- The Iliad (Homer) — quoted on war
- Twitter — where employees post
- Undertow (Lena Ortiz) — the guest's book
- We are always asked to do more with less — Marlow on budgets
- Hugging Face hack (unspecified) — an example of AI sophistication

### Quotes
- "By their fruits you shall know them" — Theo Marsh, quoting the Bible
- "Nothing like this was ever said in the episode at all" — invented
"""

TRANSCRIPT = ("and I believe in the biblical saying, by their fruit you shall know them, and what was famous. "
              "We are always asked to do more with less where what is demanded from us is way over")


class FakeModel:
    def __init__(self, inventory=None, verdict=None, claims=None):
        self.inventory = inventory if inventory is not None else {
            "works": [{"id": "W1", "type": "book", "title": "The Culture", "author": "Iain Banks"},
                      {"id": "W2", "type": "poem", "title": "The Iliad", "author": "Homer"},
                      {"id": "W3", "type": "other", "title": "Twitter", "author": ""},
                      {"id": "W4", "type": "book", "title": "Undertow", "author": "Lena Ortiz"},
                      {"id": "W5", "type": "other", "title": "We are always asked to do more with less", "author": ""},
                      {"id": "W6", "type": "not_a_work", "title": "Hugging Face hack", "author": "unspecified"}],
            "claims": [{"claim": "The city's cycling ratio was 1 to 1.4", "who": "Tobias Marlow"},
                       {"claim": "The council owns half of the parks", "who": "Rowan Ellis"}]}
        self.verdict = verdict if verdict is not None else {
            "names": [{"id": "N1", "action": "correct", "correct": "Tobias Marlow"}],
            "works": [{"id": "W2", "action": "remove"}, {"id": "W3", "action": "remove"}, {"id": "W5", "action": "move"},
                      {"id": "W6", "action": "keep"}]}
        self.claims = claims if claims is not None else {
            "claims": [{"id": "C1", "verdict": "supported", "reason": "matches the cited figure", "evidence": ["E1"]},
                       {"id": "C2", "verdict": "disputed", "reason": "sources say less", "evidence": []}]}
        self.prompts = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ask(self, prompt, limit=2500):
        self.prompts.append(prompt)
        if prompt.startswith("Below are the summary"):
            return self.inventory
        if prompt.startswith("You are checking names"):
            return self.verdict
        return self.claims


class VerifyTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "note.md"
        self.path.write_text(NOTE, encoding="utf-8")
        self.job = Path(directory.name) / "job"                  # the episode's job folder: the records live there
        self.job.mkdir()

        def wiki(query, limit=3):
            if query.startswith("Twitter"):
                return ["Twitter — American social media service"]
            if "Iliad" in query:
                return ["Iliad — Ancient Greek epic poem"]
            return [f"{query} — something"]

        def types(title):
            return {"Twitter": ["social networking service", "website"], "Iliad": ["epic poem", "literary work"]}.get(title, [])

        def book(title, author):
            if title == "The Culture Series":
                return {"title": "The Culture Series", "author": "Iain Banks", "year": 1987, "work": "/works/OL123W"}
            return {}

        for name, value in (("wikipedia", wiki), ("wikidata_types", types), ("book_record", book),
                            ("wikipedia_passages", lambda q, limit=3: []), ("studies", lambda q, limit=3: []),
                            ("web", lambda q, limit=4: [{"title": f"Source on {q[:20]}", "url": "https://example.org/a",
                                                         "snippet": "1 to 1.4"}]),
                            ("job_files", lambda text: (TRANSCRIPT, "Tobias Marlow, historian, argues it out with Rowan.")),
                            ("job_dir", lambda text: self.job)):
            patcher = mock.patch.object(podverify, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_pass(self, model=None):
        self.model = model or FakeModel()
        return podverify.verify(self.path, self.model)

    def test_full_pass(self):
        changes, claims = self.run_pass()
        text = self.path.read_text()
        self.assertEqual(1, claims)
        # names respelled everywhere, properties included
        self.assertNotIn("Tasub", text.split("Verification log")[0])
        self.assertIn('guests: ["Tobias Marlow"]', text)
        # books table: the Open Library work page; an Amazon UK book search when Open Library has no record
        self.assertIn("| Title | Author | Cited for | Find |", text)
        self.assertIn("| *The Culture Series* | Iain Banks | a positive vision | [Open Library](https://openlibrary.org/works/OL123W) |", text)
        self.assertIn("| *Undertow* | Lena Ortiz | the guest's book | [Amazon UK search](https://www.amazon.co.uk/s?k=Undertow+Lena+Ortiz&i=stripbooks) |", text)
        # The Iliad stays (its evidence says epic poem), Twitter goes (Wikidata says website)
        self.assertIn("### Other works\n- *The Iliad* (Homer) — quoted on war", text)
        self.assertNotIn("*Twitter*", text)
        self.assertIn("Removed from Works: Twitter (not a work per Wikipedia/Wikidata)", text)
        self.assertNotIn("Hugging Face hack", text.split("Verification log")[0])      # typed not_a_work, no evidence it is one
        # a quotation filed as a work moves to Quotes; quotes become callouts; the invented one goes
        self.assertIn("> [!quote] Marlow on budgets\n> \"We are always asked to do more with less\"", text)
        self.assertIn("> [!quote] Theo Marsh, quoting the Bible\n> \"by their fruit you shall know them\"", text)
        self.assertNotIn("Nothing like this was ever said", text.split("Verification log")[0])
        # claims: disputed claim without cited evidence is not checkable; supported one has a footnote
        self.assertIn("## Claims checked\n\n| Claim | Who | Verdict | Sources |", text)
        self.assertIn("| The city's cycling ratio was 1 to 1.4 | Tobias Marlow | ✅ Supported by sources — matches the cited figure | [^1] |", text)
        self.assertNotIn("could not be checked", text)
        self.assertNotIn("The council owns half of the parks", text)            # unchecked claims are left out entirely
        self.assertIn("[^1]: [Source on The city's cycling r](https://example.org/a) — 1 to 1.4", text)
        # order: themes, claims, references, log, footnotes
        self.assertLess(text.index("## Key themes"), text.index("## Claims checked"))
        self.assertLess(text.index("## Claims checked"), text.index("## References"))
        self.assertLess(text.index("## References"), text.index("> [!info]- Verification log"))
        self.assertLess(text.index("> [!info]- Verification log"), text.index("[^1]:"))
        record = json.loads((self.job / "verify-record.json").read_text())    # processing state stays outside the note
        self.assertEqual((changes, 1, 0), (record["changes"], record["claims_checked"], record["claims_disputed"]))

    def test_ideas_are_linked_corrected_moved_and_kept(self):
        self.path.write_text(NOTE.replace(
            "### Quotes", "- Broken window theory — Dana on small offences\n\n### Ideas\n"
            "- Goodharts law (Charles Goodhart) — on targets\n- Polymarket — a betting site\n"
            "- Peelian principles (unspecified) — on policing by consent\n- Kindness — a virtue\n"
            "- Victoria Cross — the highest award for gallantry\n"
            "- The Hoffman principle — never heard of it\n\n### Quotes"), encoding="utf-8")
        pages = {"Broken window theory": ["Broken windows theory — Criminological theory"],
                 "Peelian principles": ["Peelian principles — Ethical tenets for an approved police force"],
                 "Kindness": ["Kindness — Topics referred to by the same term"],
                 "Victoria Cross": ["Victoria Cross — Highest award for gallantry"],
                 "Goodharts law": ["Goodhart's law — Adage about statistical measures"],
                 "Polymarket": ["Polymarket — Prediction market platform"]}

        def wiki(query, limit=3):
            for start, hits in pages.items():
                if query.startswith(start):
                    return hits
            return [] if query.startswith("The Hoffman") else [f"{query} — something"]

        with mock.patch.object(podverify, "wikipedia", wiki):
            self.run_pass()
        text = self.path.read_text()
        self.assertIn("### Ideas\n- [Goodhart's law](https://en.wikipedia.org/wiki/Goodhart%27s_law) (Charles Goodhart) — on targets\n"
                      "- [Peelian principles](https://en.wikipedia.org/wiki/Peelian_principles) — on policing by consent\n"
                      "- Kindness — a virtue\n"                                    # a page of meanings: no link
                      "- Victoria Cross — the highest award for gallantry\n"       # an honour, not an idea: no link
                      "- The Hoffman principle — never heard of it\n"
                      "- [Broken windows theory](https://en.wikipedia.org/wiki/Broken_windows_theory) — Dana on small offences", text)
        self.assertIn("Moved to Ideas: Broken window theory (Broken windows theory)", text)
        self.assertIn("Removed from Ideas: Polymarket", text)
        self.assertLess(text.index("### Ideas"), text.index("### Quotes"))

    def test_a_claim_may_cite_only_its_own_evidence(self):
        model = FakeModel(claims={"claims": [{"id": "C1", "verdict": "supported", "reason": "ok", "evidence": ["E1"]},
                                             {"id": "C2", "verdict": "supported", "reason": "borrowed", "evidence": ["E1"]}]})
        self.run_pass(model)
        text = self.path.read_text()
        self.assertIn("The city's cycling ratio", text)
        self.assertNotIn("The council owns half of the parks", text)                  # C2 borrowed C1's source: no verdict

    def test_no_claims_section_when_nothing_was_checked(self):
        model = FakeModel(claims={"claims": [{"id": "C1", "verdict": "not_checkable", "reason": "", "evidence": []},
                                             {"id": "C2", "verdict": "disputed", "reason": "x", "evidence": []}]})
        changes, claims = self.run_pass(model)
        text = self.path.read_text()
        self.assertEqual(0, claims)
        self.assertNotIn("## Claims checked", text)
        self.assertNotIn("could not be checked", text)

    def test_the_verification_record_stays_outside_the_note(self):
        self.run_pass()
        text = self.path.read_text()
        self.assertEqual((-1, 0), self.run_pass())                           # the record knows this very note
        for gone in ("verified:", "verify_changes:", "claims_checked:", "claims_disputed:"):
            self.assertNotIn(gone, text.split("---")[1])
        self.assertNotIn('updated: "2026-10-07T10:00:00"', text)             # verification is a change
        self.assertIn('created: "2026-10-07T10:00:00"', text)
        record = json.loads((self.job / "verify-record.json").read_text())
        self.assertEqual(1, record["claims_checked"])
        self.assertIn("verified_at", record)

    def test_already_verified_note_is_skipped(self):
        self.run_pass()
        before = self.path.read_text()
        self.assertEqual((-1, 0), self.run_pass())
        self.assertEqual(before, self.path.read_text())

    def test_without_a_model_the_note_is_still_rendered_and_nothing_removed(self):
        changes, claims = self.run_pass(FakeModel(inventory={}, verdict={}, claims={}))
        text = self.path.read_text()
        self.assertEqual(0, claims)
        self.assertIn("*Twitter*", text)
        self.assertIn("Tobias Marlough", text)
        self.assertNotIn("## Claims checked", text)
        self.assertIn("### Quotes", text)

    def test_correction_to_a_different_title_or_name_is_refused(self):
        model = FakeModel(verdict={"names": [{"id": "N1", "action": "correct", "correct": "Tobias Marlow Senior"}],
                                   "works": [{"id": "W1", "action": "correct", "title": "Instrumental convergence"}]})
        self.run_pass(model)
        text = self.path.read_text()
        self.assertIn("*The Culture Series*", text)
        self.assertNotIn("Instrumental", text)
        self.assertNotIn("Senior", text)

    def test_a_participant_is_who_cited_a_work_not_its_author(self):
        front = 'show: "The Daily Courier"\nhosts: ["Clara Benton", "Hugo Marsh"]\nguests:\n- Edmund Hale\n'
        self.assertEqual(("The Daily Courier", ["Clara Benton", "Hugo Marsh", "Edmund Hale"]), podverify.front_people(front))
        people = ["Hugo Marsh", "Edmund Hale"]
        pages = {"Harry Potter": ["Harry Potter — Series of fantasy novels by J. K. Rowling"],
                 "Sunday Courier": ["The Sunday Courier — British newspaper"], "My Report": []}
        with mock.patch.object(podverify, "book_record", lambda title, author: {"title": title} if author == "Edmund Hale" else {}), \
                mock.patch.object(podverify, "wikipedia", lambda q, n=3: pages.get(q, [])):
            self.assertEqual("J. K. Rowling", podverify.cited_by_not_written_by("Harry Potter", "Hugo Marsh", people))
            self.assertEqual("", podverify.cited_by_not_written_by("Sunday Courier", "Hugo Marsh", people))   # the citer
            self.assertIsNone(podverify.cited_by_not_written_by("The Lighthouse Keepers", "Edmund Hale", people))  # his own book
            self.assertIsNone(podverify.cited_by_not_written_by("My Report", "Hugo Marsh", people))   # no evidence: as it is
            self.assertIsNone(podverify.cited_by_not_written_by("Harry Potter", "J. K. Rowling", people))  # not a participant

    def test_episode_date_in_any_quoting_or_from_the_header(self):
        self.assertEqual("2026-09-09", podverify.episode_date('---\npublished: "2026-09-09T16:47:41"\n---\n'))
        self.assertEqual("2026-09-09", podverify.episode_date("---\npublished: '2026-09-09'\n---\n"))
        self.assertEqual("2026-09-02", podverify.episode_date("---\nx: y\n---\n# T\n\n**Show** · 2026-09-02 · ⏱ 1h\n"))

    def test_a_broken_bracket_costs_one_field_not_the_answer(self):
        answer = podverify.parse_answer('json\n{"claims": [{"id": "C1", "verdict": "supported", "reason": "Confirmed.", '
                                        '"evidence": ["E2", "E4"\n }\n },\n {"id": "C2", "verdict": "not_checkable", '
                                        '"reason": "No source.", "evidence": ["E5"]}]}')
        self.assertEqual([{"id": "C1", "verdict": "supported", "reason": "Confirmed.", "evidence": ["E2", "E4"]},
                          {"id": "C2", "verdict": "not_checkable", "reason": "No source.", "evidence": ["E5"]}],
                         answer["claims"])

    def test_relevance_filter_drops_adverts(self):
        hits = [{"title": "Picture Frames in Any Size", "snippet": "Low prices", "url": "u1"},
                {"title": "Young software engineers employment falls 20%", "snippet": "Stanford study", "url": "u2"}]
        self.assertEqual(["u2"], [h["url"] for h in podverify.relevant("Stanford young software engineers employment 20%", hits)])

    def test_named_papers_the_publisher_and_non_works_both_checks_reject_are_removed(self):
        self.path.write_text(NOTE.replace('show: "Show"', 'show: "Show"\npublisher: "The Spectator"').replace(
            "- Hugging Face hack (unspecified) — an example of AI sophistication",
            "- The Times — where the column ran\n- The Spectator — the host's magazine\n"
            "- Operation Sealion was a planned invasion — inspiration for the novel\n"
            "- Operation Seelau is a historical operation mentioned as the source of the novel."), encoding="utf-8")
        pages = {"The Times": ["The Times — British daily national newspaper"],
                 "Operation Sealion was a planned invasion": ["Operation Sea Lion — German plan to invade Britain"]}
        inventory = FakeModel().inventory
        inventory["works"] = inventory["works"][:5] + [
            {"id": "W6", "type": "other", "title": "The Times", "author": ""},
            {"id": "W7", "type": "other", "title": "The Spectator", "author": ""},
            {"id": "W8", "type": "not_a_work", "title": "Operation Sealion was a planned invasion", "author": ""},
            {"id": "W9", "type": "other", "title": "Operation Seelau", "author": ""}]
        verdict = FakeModel().verdict
        verdict["works"] = verdict["works"][:3] + [{"id": "W6", "action": "remove"}, {"id": "W7", "action": "keep"},
                                                   {"id": "W8", "action": "remove"}, {"id": "W9", "action": "remove"}]
        lookup = lambda q, limit=3: [] if q.startswith("Operation Seelau") else pages.get(q, [f"{q} — something"])
        with mock.patch.object(podverify, "wikipedia", lookup):
            self.run_pass(FakeModel(inventory=inventory, verdict=verdict))
        kept = self.path.read_text().split("---", 2)[2].split("Verification log")[0]   # the note's text
        for gone in ("The Times", "The Spectator", "Operation Sealion", "Operation Seelau"):
            self.assertNotIn(gone, kept)
        self.assertIn("The Iliad", kept)                          # the model's "remove" of a real work is still refused

    def test_failed_lookup_never_removes(self):
        with mock.patch.object(podverify, "wikipedia", lambda q, limit=3: []):
            self.run_pass()
        self.assertIn("Hugging Face hack", self.path.read_text().split("Verification log")[0])
        self.assertIn("> - Wikipedia did not answer; works were left as they were.", self.path.read_text())

    def test_rate_limit_waits_as_asked_then_succeeds(self):
        import io, urllib.error
        limited = urllib.error.HTTPError("u", 429, "Too Many Requests", {"Retry-After": "7"}, None)
        answers = [limited, io.BytesIO(b'{"ok": 1}')]

        def urlopen(request, timeout):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with mock.patch.object(podverify.urllib.request, "urlopen", urlopen), \
                mock.patch.object(podverify.time, "sleep") as sleep:
            self.assertEqual({"ok": 1}, podverify.http_json("https://en.wikipedia.org/w/api.php"))
        self.assertIn(mock.call(7), sleep.call_args_list)

    def test_host_still_rate_limiting_is_skipped_for_the_rest_of_the_pass(self):
        import urllib.error
        calls = []

        def urlopen(request, timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError("u", 429, "Too Many Requests", {"Retry-After": "7"}, None)

        self.addCleanup(podverify.THROTTLED.clear)
        with mock.patch.object(podverify.urllib.request, "urlopen", urlopen), mock.patch.object(podverify.time, "sleep"):
            for _ in range(2):
                with self.assertRaises(urllib.error.URLError):
                    podverify.http_json("https://en.wikipedia.org/w/api.php?q=x")
        self.assertEqual(3, len(calls))

    def test_guards(self):
        self.assertTrue(podverify.proven_not_a_work("Polymarket", ["Polymarket — Prediction market platform"], []))
        self.assertTrue(podverify.proven_not_a_work("X", [], ["website", "social networking service"]))
        self.assertFalse(podverify.proven_not_a_work("The Iliad", ["Iliad — Ancient Greek epic poem"], ["epic poem"]))
        self.assertFalse(podverify.proven_not_a_work("Anthropic threat report", ["Anthropic — American AI company"], []))
        self.assertFalse(podverify.proven_not_a_work("Anything", [], []))
        self.assertTrue(podverify.same_title("Brink Inside the Race", "Brink: Inside the Race"))
        self.assertFalse(podverify.same_title("The Paperclip Problem", "Instrumental convergence"))
        self.assertFalse(podverify.same_title("Swan Song", "Swan song now glad whooping"))
        self.assertFalse(podverify.same_title("National Rally", "National Rally (Le Pen)"))
        self.assertFalse(podverify.proven_not_a_work("Section 230", ["Section 230 — United States law on online platforms"], []))
        self.assertTrue(podverify.same_title("Undertow", "Undertow: The AI Industry's Race to Replace Us"))
        self.assertTrue(podverify.author_agrees("Homer", ["Homer"]))
        self.assertFalse(podverify.author_agrees("Jeffrey Archer", ["Harry M. Blake"]))
        self.assertFalse(podverify.readable("Ὅμηρος"))
        self.assertTrue(podverify.readable("Iain Banks"))
        self.assertEqual("world", podverify.bare_title_words("The World"))
        # no shop link for scripture or for forgeries and hate texts; ordinary books keep theirs
        self.assertTrue(podverify.no_shop_link({"title": "Ecclesiastes", "hits": [], "types": []}))
        self.assertTrue(podverify.no_shop_link({"title": "The Old Testament", "hits": [], "types": []}))
        self.assertTrue(podverify.no_shop_link({"title": "The Donation of Constantine", "types": [], "hits": [
            "The Donation of Constantine — Forged Roman imperial decree"]}))
        self.assertFalse(podverify.no_shop_link({"title": "Numbers Don't Lie", "hits": ["Numbers Don't Lie — Book by Vaclav Smil"], "types": []}))
        self.assertFalse(podverify.no_shop_link({"title": "The Prince", "hits": ["The Prince — Political treatise"], "types": []}))
        rovelli = lambda title: {"title": title, "author": "Carlo Rovelli"}
        self.assertTrue(podverify.same_work(rovelli("On the Equality of All Things: Lessons on Physics and Philosophy"),
                                            rovelli("On the Equality of Things")))
        self.assertTrue(podverify.same_work(rovelli("On The Equality of All Things Lessons on Physics and Philosophy"),
                                            rovelli("On The Equality of Things")))                 # no colon
        self.assertFalse(podverify.same_work(rovelli("Reality Is Not What It Seems"), rovelli("The Order of Time")))
        # "by Author" in an entry; a byline needs a full name ("Stand by Me" stays a title)
        self.assertEqual(("A Study in Scarlet", "Arthur Conan Doyle", ""),
                         podverify.work_title_author("A Study in Scarlet by Arthur Conan Doyle"))
        self.assertEqual(("Stand by Me", "", "a song"), podverify.work_title_author("Stand by Me — a song"))
        # the author a Wikipedia description gives a work, and none for things that are not works by someone
        self.assertEqual("Terry Pratchett, Neil Gaiman", podverify.author_from_description(
            "Good Omens — 1990 novel by Terry Pratchett and Neil Gaiman"))
        self.assertEqual("Friedrich von Hayek", podverify.author_from_description("The Road to Serfdom — Book by Friedrich von Hayek"))
        self.assertEqual("", podverify.author_from_description("Swan song — Final gesture or effort"))
        self.assertEqual("", podverify.author_from_description("Leviathan — Biblical sea monster"))
        self.assertTrue(podverify.spelling_variant("Tobias Marlough", "Tobias Marlow"))
        self.assertFalse(podverify.spelling_variant("President Macron", "Emmanuel Macron"))
        self.assertFalse(podverify.spelling_variant("Mustafa Kemal Pasha", "Mustafa Kemal Atatürk"))
        self.assertFalse(podverify.spelling_variant("Ayatollah Khomeini", "Ruhollah Khomeini"))
        self.assertTrue(podverify.spelling_variant("Alistair Burnet", "Alastair Burnet"))
        self.assertTrue(podverify.spelling_variant("Brink Inside", "Brink: Inside"))
        # a broken answer keeps the items that parse, each under its own list
        broken = '{"names": [{"id": "N1", "action": "keep"}], "works": [{"id": "W1", "action": "keep", "title": "a "b" c"}, {"id": "W2", "action": "remove"}]}'
        self.assertEqual({"names": [{"id": "N1", "action": "keep"}], "works": [{"id": "W2", "action": "remove"}]},
                         podverify.parse_answer("```json\n" + broken + "\n```"))
        self.assertFalse(podverify.whole_json(broken))                  # a salvaged answer is recorded as partly read
        self.assertTrue(podverify.whole_json('{"claims": []}'))
        self.assertEqual("Books", podverify.group_for("", ["novel series"]))
        self.assertEqual("Films & TV", podverify.group_for("documentary", []))
        self.assertEqual("Laws", podverify.group_for("law", []))
        self.assertEqual("Brotherhood of Italy", podverify.bare_title("Brotherhood of Italy (Meloni)"))
        self.assertEqual("IDI study (2023)", podverify.bare_title("IDI study (2023)"))
        self.assertEqual(("IDI study (2023)", "", "cited"), podverify.work_title_author("IDI study (2023) — cited"))
        self.assertTrue(podverify.named_author("Brett McGurk", "Brink Inside the Race by Brett McGurk", "Brink"))
        self.assertTrue(podverify.named_author("Harry", "Swan Song — cited as a memoir by Harry", "Swan Song"))
        self.assertFalse(podverify.named_author("ISM", "ISM manufacturing — cited by ISM", "ISM manufacturing"))
        self.assertTrue(podverify.named_author("Jeffrey Archer", "Cain and Abel — Jeffrey Archer cites its reprints", "Cain and Abel"))
        self.assertFalse(podverify.named_author("New York Times", "New York Times cover of 9/11 — cited", "New York Times cover of 9/11"))
        self.assertFalse(podverify.named_author("Iain Banks", "The Culture — a positive vision", "The Culture"))
        self.assertEqual("Other works", podverify.group_for("poem", ["epic poem"]))

    def test_book_record_finds_the_open_library_work(self):
        calls = []
        works = {"stalin": {"key": "/works/OL1W", "title": "Stalin", "subtitle": "The Court of the Red Tsar",
                            "author_name": ["Arthur Conan Doyle"]},
                 "odyssey": {"key": "/works/OL2W", "title": "The Odyssey", "author_name": ["Ὅμηρος"],
                             "author_alternative_name": ["Homer"], "edition_count": 240},
                 "odyssey ": {"key": "/works/OL9W", "title": "The Odyssey", "author_name": ["Ὅμηρος"],
                              "author_alternative_name": ["Homer"], "edition_count": 20},
                 "world": {"key": "/works/OL3W", "title": "World", "author_name": ["Arthur Conan Doyle"]},
                 "brink": {"key": "/works/OL4W", "title": "Brinkmanship", "author_name": ["Someone Else"]}}

        def catalogue(url, timeout=15):
            calls.append(url)
            return {"docs": [doc for word, doc in works.items() if f"title={word.strip()}" in url.lower()]}

        with mock.patch.object(podverify, "http_json", catalogue):
            stalin = REAL_BOOK_RECORD("Stalin: The Court of the Red Tsar", "Arthur Conan Doyle")
            odyssey = REAL_BOOK_RECORD("The Odyssey", "Homer")
            world = REAL_BOOK_RECORD("The World: A Family History", "Arthur Conan Doyle")
            brink = REAL_BOOK_RECORD("Brink", "Brett McGurk")
        self.assertIn("title=stalin&", calls[0])                                  # the bare main title
        self.assertEqual(("Stalin: The Court of the Red Tsar", "/works/OL1W"), (stalin["title"], stalin["work"]))
        self.assertEqual("/works/OL2W", odyssey["work"])        # "Homer" among alternative names; the record with most editions
        self.assertEqual("/works/OL3W", world["work"])                             # "World", no article, no subtitle
        self.assertEqual({}, brink)                                                # different title and author
        self.assertEqual(("Open Library", "https://openlibrary.org/works/OL1W"),
                         podverify.book_link("Stalin", "Arthur Conan Doyle", "/works/OL1W"))
        self.assertEqual(("Amazon UK search", "https://www.amazon.co.uk/s?k=Brink+Brett+McGurk&i=stripbooks"),
                         podverify.book_link("Brink", "Brett McGurk"))

    def test_latest_english_edition(self):
        self.assertEqual((2017, 5, 16), podverify.publication_date("May 16, 2017"))
        self.assertEqual((2016, 1, 28), podverify.publication_date("2016-01-28"))
        self.assertEqual((2016, 0, 0), podverify.publication_date("2016"))
        self.assertEqual((0, 0, 0), podverify.publication_date("unknown"))
        entries = [
            {"key": "/books/OL1M", "publish_date": "2026", "languages": [{"key": "/languages/tur"}], "isbn_13": ["9789750842016"]},
            {"key": "/books/OL2M", "publish_date": "2025", "physical_format": "ebook", "languages": [{"key": "/languages/eng"}]},
            {"key": "/books/OL3M", "publish_date": "May 16, 2017", "physical_format": "paperback", "isbn_10": ["0307280519"]},
            {"key": "/books/OL4M", "publish_date": "2017", "languages": [{"key": "/languages/eng"}]},
            {"key": "/books/OL5M", "publish_date": "2012", "physical_format": "hardcover", "languages": [{"key": "/languages/eng"}]},
            {"key": "/books/OL6M", "publish_date": "2018", "isbn_10": ["3596176328"]},          # German ISBN, no language
            {"key": "/books/OL7M", "publish_date": "April 1, 2026", "languages": [{"key": "/languages/eng"}],
             "isbn_13": ["9789388843089"]},                                                     # English, Indian reprint
            {"key": "/books/OL8M", "publish_date": "2021", "publishers": ["Independently Published"],
             "languages": [{"key": "/languages/eng"}], "isbn_13": ["9798709951761"]},          # print-on-demand
        ]
        with mock.patch.object(podverify, "http_json", lambda url, timeout=15: {"entries": entries}):
            # not the Turkish or Indian 2026, the 2025 e-book, the German 2018 or the self-published 2021:
            # the English paperback of May 2017
            self.assertEqual("/books/OL3M", podverify.latest_english_edition("/works/OL1W"))
        self.assertEqual(("Open Library", "https://openlibrary.org/works/OL1W?edition=key:/books/OL3M"),
                         podverify.book_link("The Romanovs", "Arthur Conan Doyle", "/works/OL1W", "/books/OL3M"))

    def test_disputes_need_the_same_period_and_a_real_contradicting_sentence(self):
        self.assertFalse(podverify.same_period("Q2 GDP was revised up to 2.2%",
                                               ["Q2 2018 growth was 4.2%"], "2026-10-02"))
        self.assertTrue(podverify.same_period("Q2 GDP was revised up to 2.2%",
                                              ["BEA: second estimate for Q2 2026 shows 2.2%"], "2026-10-02"))
        self.assertTrue(podverify.same_period("In 2018 growth hit 4.2%", ["Q2 2018 growth was 4.2%"], "2026-10-02"))
        self.assertTrue(podverify.same_period("Lighthouses were relit", ["168 lighthouses were relit"], "2026-10-07"))

        class Judge:
            def __init__(self, answer):
                self.answer = answer

            def ask(self, prompt, limit=2500):
                return self.answer

        sources = ["Lighthouse survey — 251 lighthouses were inspected and 168 were returned to service."]
        claim = "All lighthouses have been returned to service"
        self.assertFalse(podverify.confirmed_contradiction(Judge({"contradicts": False, "quote": ""}), claim, sources, ""))
        self.assertFalse(podverify.confirmed_contradiction(       # an invented sentence is not evidence
            Judge({"contradicts": True, "quote": "Twenty lighthouses remain dark today"}), claim, sources, ""))
        self.assertTrue(podverify.confirmed_contradiction(
            Judge({"contradicts": True, "quote": "251 lighthouses were inspected and 168 were returned to service."}), claim, sources, ""))

    def test_quotes_keep_only_the_words_said(self):
        self.assertEqual(("snap", "by their fruit you shall know them"),       # close: the transcript's own words
                         podverify.check_quote('"By their fruits you shall know them" — X', TRANSCRIPT))
        self.assertEqual("remove", podverify.check_quote('"Completely absent words here today" — X', TRANSCRIPT)[0])
        self.assertEqual("remove", podverify.check_quote('"We are put in double standards always" — X', TRANSCRIPT)[0])
        said = "and he said never never never give in to anyone at all"
        self.assertEqual(("keep", None),                                     # spoken repetition is the quote
                         podverify.check_quote('"Never never never give in to anyone" — X', said))
        self.assertEqual(("keep", None), podverify.check_quote('"give in"', said))      # short, said as such
        self.assertEqual(("remove", None), podverify.check_quote('"give up"', said))


if __name__ == "__main__":
    unittest.main()


class BookResolverTests(unittest.TestCase):
    """Books are named by the web (search results read by the model), never by memory."""

    class Answer:
        def __init__(self, answer):
            self.answer = answer

        def ask(self, prompt, limit=2500):
            return self.answer

    def test_a_described_or_misheard_book_gets_its_real_name(self):
        hits = ["Pale Blue Dot: A Vision of the Human Future in Space — https://sagan.example/x"]
        with mock.patch.object(podverify, "book_search", lambda q, limit=5: hits):
            named = podverify.resolve_book("Sagan's book about the pale blue dot", "Carl Sagan", "on our place in space",
                                           self.Answer({"title": "Pale Blue Dot",
                                                        "author": "Carl Sagan"}))
        self.assertEqual(("Pale Blue Dot", "Carl Sagan"), named)

    def test_an_unrelated_answer_or_no_results_leave_the_book_as_heard(self):
        with mock.patch.object(podverify, "book_search", lambda q, limit=5: ["Beagle Puppy Grooming Tips — https://petshop.example"]):
            self.assertIsNone(podverify.resolve_book("The Voyage of the Beagle", "Charles Darwin", "",
                                                     self.Answer({"title": "Puppy Grooming", "author": "Pet Shop"})))
        with mock.patch.object(podverify, "book_search", lambda q, limit=5: []):
            self.assertIsNone(podverify.resolve_book("Anything", "Someone", "", self.Answer({})))
        searched = []
        with mock.patch.object(podverify, "book_search", lambda q, limit=5: searched.append(q) or []):
            podverify.resolve_book("The Hollow Lantern", "Jamie Fenwick", "", self.Answer({}))
        self.assertEqual(["The Hollow Lantern Jamie Fenwick book", "The Hollow Lantern book"], searched)   # the title alone next


class TidyBookTests(unittest.TestCase):

    def test_shop_spellings_are_made_plain(self):
        self.assertEqual(("Master and Commander", "Patrick O'Brian"),
                         podverify.tidy_book("Master and Commander (Aubrey & Maturin, #1)", "O'Brian, Patrick"))
        self.assertEqual(("The Great Lighthouses: The Story of the North Sea", "Jules Verne"),
                         podverify.tidy_book("THE GREAT LIGHTHOUSES: THE STORY OF THE NORTH SEA", "JULES VERNE"))
        self.assertEqual(("Middlemarch", "George Eliot"), podverify.tidy_book("Middlemarch", "George Eliot"))
        self.assertEqual(("The Lighthouse Keepers", "Edmund Rhys Hale, Clara Benton"),        # ISBN gone; two authors turned round
                         podverify.tidy_book("The Lighthouse Keepers: 9781234567897", "Hale, Edmund Rhys, Benton, Clara"))
        self.assertEqual(("Good Omens", "Terry Pratchett, Neil Gaiman"), podverify.tidy_book("Good Omens", "Terry Pratchett, Neil Gaiman"))
        # shop listings: no edition, no blurb for a subtitle; a real subtitle stays
        self.assertEqual("Lighthouse Engineering", podverify.tidy_book("Lighthouse Engineering, 3rd Edition", "Clara Benton")[0])
        self.assertEqual("The Tide Atlas", podverify.tidy_book("The Tide Atlas (Second edition)", "Clara Benton")[0])
        self.assertEqual("Harbour Lights", podverify.tidy_book(
            "Harbour Lights: A gripping memoir, a “taut thriller of high stakes” (Jo Example), and more", "Clara Benton")[0])
        self.assertEqual("Harbour Lights", podverify.tidy_book(
            "Harbour Lights: The #1 bestselling memoir", "Clara Benton")[0])
        for real in ("Superintelligence: Paths, Dangers, Strategies", "The Second Coming", "1929: Inside the Greatest Crash",
                     "Edition: A Novel"):
            self.assertEqual(real, podverify.tidy_book(real, "Clara Benton")[0])


class BraveSearchTests(unittest.TestCase):

    def test_results_are_plain_title_url_snippet(self):
        import io
        payload = {"web": {"results": [{"title": "Lighthouse <strong>Museum</strong>", "url": "https://x.org/a",
                                        "description": "Visitors &amp; <strong>opening hours</strong>"}]}}
        response = io.BytesIO(json.dumps(payload).encode())
        response.__enter__ = lambda self=response: self
        response.__exit__ = lambda *a: False
        with mock.patch.object(podverify, "brave_key", return_value="k"), \
                mock.patch.object(podverify.urllib.request, "urlopen", return_value=response), \
                mock.patch.object(podverify.time, "sleep"):
            hits = podverify.web("Lighthouse museum visitors", 4)
        self.assertEqual([{"title": "Lighthouse Museum", "url": "https://x.org/a", "snippet": "Visitors & opening hours"}], hits)

    def test_books_are_searched_on_brave_once_per_query(self):
        calls = []
        hits = [{"title": "Pale Blue Dot", "url": "https://x.org/b", "snippet": ""}, {"title": "", "url": "https://x.org/c", "snippet": ""}]
        podverify._SEARCHED.clear()
        with mock.patch.object(podverify, "web", lambda q, limit=4: calls.append(q) or hits):
            self.assertEqual(["Pale Blue Dot — https://x.org/b"], podverify.book_search("Pale Blue Dot Carl Sagan book"))
            podverify.book_search("Pale Blue Dot Carl Sagan book")
        self.assertEqual(["Pale Blue Dot Carl Sagan book"], calls)
        podverify._SEARCHED.clear()

    def test_no_key_means_no_web_evidence(self):
        with mock.patch.object(podverify, "brave_key", return_value=""):
            self.assertEqual([], podverify.web("anything"))
