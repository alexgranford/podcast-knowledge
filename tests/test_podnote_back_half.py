#!/usr/bin/env python3
"""Unit tests for podnote's back half: the pure functions that shape the note without a model."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import notecheck
import podnote


class NamesFileTestCase(unittest.TestCase):
    """Point podnote at a temporary names.txt for the duration of one test."""

    def use_names(self, text: str) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "names.txt"
        path.write_text(text, encoding="utf-8")
        patcher = mock.patch.object(podnote, "NAMES_FILE", path)
        patcher.start()
        self.addCleanup(patcher.stop)


class SectionsTests(unittest.TestCase):

    def transcript(self, text: str) -> dict:
        return {"chunks": [{"text": text}]}

    def test_short_transcript_is_one_part(self):
        self.assertEqual(["One. Two three."], podnote.sections(self.transcript("One.   Two\nthree.")))

    def test_parts_break_at_sentence_boundaries_within_the_word_limit(self):
        sentence = " ".join(["word"] * 99) + "."
        parts = podnote.sections(self.transcript(" ".join([sentence] * 40)))   # 4,000 words
        self.assertGreater(len(parts), 1)
        for part in parts[:-1]:
            self.assertLessEqual(len(part.split()), podnote.SECTION_WORDS)
        # the last part may absorb a remainder under a quarter of the limit rather than stand alone
        self.assertLessEqual(len(parts[-1].split()), podnote.SECTION_WORDS + podnote.SECTION_WORDS // 4)
        self.assertTrue(all(part.endswith(".") for part in parts))

    def test_unpunctuated_run_is_cut_at_the_word_limit(self):
        parts = podnote.sections(self.transcript(" ".join(["word"] * (podnote.SECTION_WORDS * 2))))
        self.assertEqual([podnote.SECTION_WORDS, podnote.SECTION_WORDS], [len(p.split()) for p in parts])

    def test_small_remainder_joins_the_last_part(self):
        words = podnote.SECTION_WORDS + podnote.SECTION_WORDS // 8
        parts = podnote.sections(self.transcript(" ".join(["word"] * words)))
        self.assertEqual([words], [len(p.split()) for p in parts])


class SplitReferencesTests(unittest.TestCase):

    def test_references_label_on_its_own_line(self):
        points, refs = podnote.split_references("**Topic:** x\n- a point\n**References:**\n- [work] A\n- [quote] \"b c d e\" — X")
        self.assertEqual("**Topic:** x\n- a point", points)
        self.assertEqual(["[work] A", "[quote] \"b c d e\" — X"], refs)

    def test_first_entry_on_the_label_line_is_kept(self):
        self.assertEqual(["[work] A", "[work] B"], podnote.split_references("- p\n**References:** [work] A\n- [work] B")[1])

    def test_none_and_no_label(self):
        self.assertEqual([], podnote.split_references("- p\n**References:**\n- none")[1])
        self.assertEqual(("- p", []), podnote.split_references("- p"))

    def test_a_point_that_starts_with_the_word_references_is_not_the_label(self):
        notes = "- References to Orwell recur.\n**References:**\n- [work] 1984 (George Orwell) — cited"
        points, refs = podnote.split_references(notes)
        self.assertEqual("- References to Orwell recur.", points)
        self.assertEqual(["[work] 1984 (George Orwell) — cited"], refs)



class ReferencesSectionTests(unittest.TestCase):

    def test_works_and_quotes_in_episode_order(self):
        section = podnote.references_section([
            "[work] Undertow (Lena Ortiz) — the guest's book",
            "[quote] \"By their fruits you shall know them\" — Theo Marsh, quoting the Bible",
            "[work] The Culture series (Iain Banks) — a positive vision",
            "[idea] Broken windows theory — Dana on small offences inviting serious crime",
        ])
        self.assertEqual(
            "## References\n\n### Works\n- Undertow (Lena Ortiz) — the guest's book\n"
            "- The Culture series (Iain Banks) — a positive vision\n\n"
            "### Ideas\n- Broken windows theory — Dana on small offences inviting serious crime\n\n"
            "### Quotes\n- \"By their fruits you shall know them\" — Theo Marsh, quoting the Bible",
            section)

    def test_duplicates_untagged_and_one_word_quotes_are_dropped(self):
        section = podnote.references_section([
            "[work] Undertow (Lena Ortiz) — the guest's book",
            "[work] Undertow (Lena Ortiz) — cited again later",
            "[quote] \"pogroms\" — a term used by a speaker",
            "an untagged line",
        ])
        self.assertEqual("## References\n\n### Works\n- Undertow (Lena Ortiz) — the guest's book", section)

    def test_nothing_cited_means_no_section(self):
        self.assertEqual("", podnote.references_section(["[quote] \"too short\" — X", "plain"]))


class CleanDescriptionTests(unittest.TestCase):

    def test_guest_introduction_glued_to_a_sponsor_link_survives(self):
        raw = ("Try Brand X (BUY ONE GET ONE FREE ONLY!): https://brand.example/offerIn this episode, "
               "I sit down with author Lena Ortiz to debate AI risk. Lena makes the case for caution."
               "Our show is independently supported by you, consider signing up to our substack here: "
               "https://show.example/Chapters 00:00 Could AI Cause Extinction? 04:42 What Happens When Agents Escape?")
        text = podnote.clean_description(raw)
        self.assertIn("I sit down with author Lena Ortiz", text)
        self.assertIn("Lena makes the case for caution.", text)
        self.assertIn("04:42 What Happens When Agents Escape?", text)
        self.assertNotIn("BUY ONE", text)
        self.assertNotIn("substack", text.lower())
        self.assertNotIn("http", text)

    def test_chapter_list_survives_and_follow_lines_go(self):
        raw = ("(0:00) Intros! (2:01) The summit and the accord (17:57) Data centers, Example.gov, doomer messaging "
               "(1:09:28) Media coverage of a hijacking Follow the crew: https://x.example/a")
        text = podnote.clean_description(raw)
        self.assertIn("Example.gov", text)
        self.assertIn("Media coverage of a hijacking", text)
        self.assertNotIn("crew", text)

    def test_advertisers_are_found_and_kept_out_of_the_description(self):
        raw = ("Today: five rules for better AI writing.\n"
               "NEXT COHORT - Executive Agent Leadership - Returns in September - https://training.example/\n"
               "Brought to you by:\n"
               "Northwind \u2013 Research from Northwind and the University of Exampleton shows the highest-impact AI users treat AI like a reasoning partner. "
               "Learn more at \u2060https://northwind.example/research\u2060\n"
               "Harbourline - Invest in the AI ecosystem. https://www.harbourline.example/brief\n"
               "Gears & Crayons - Cloud-native AI solutions that power results https://gearsandcrayons.example/\n"
               "\n"
               "This episode is sponsored by Clear Headlines and Nomad Savings.\n"
               "Sponsors: Taskly - https://taskly.example/show and get 10% off. Voxa - Get 14 days free at https://voxa.example/host")
        sponsors = podnote.sponsors_in(raw)
        for brand in ("Northwind", "Harbourline", "Gears & Crayons", "Clear Headlines", "Nomad Savings", "Taskly", "Voxa"):
            self.assertIn(brand, sponsors)
        self.assertNotIn("Today", sponsors)
        description = podnote.clean_description(raw, drop=sponsors)
        themes = ("- **The amplification strategy** — A Northwind and University of Exampleton study found top performers guide AI.\n"
                  "- **Consulting** — Firms such as Northwind are cutting graduate hiring.\n"
                  "- **Writing** — Brevity beats verbose output.")
        kept = podnote.drop_native_ads(themes, podnote.sponsor_items(raw))
        self.assertNotIn("amplification", kept)                         # the ad read as a study
        self.assertIn("Firms such as Northwind", kept)                        # a plain mention of the brand stays
        self.assertIn("Brevity", kept)
        self.assertTrue(podnote.native_ad("[work] A study by Northwind and the University of Exampleton on AI users",
                                          podnote.sponsor_items(raw)))
        self.assertIn("five rules for better AI writing", description)
        self.assertNotIn("Harbourline", description)

    def test_length_is_capped(self):
        self.assertLessEqual(len(podnote.clean_description("A real sentence about the topic here. " * 200)), 1500)


class NamesRulesTests(NamesFileTestCase):

    def test_replacement_is_case_insensitive_and_whole_word(self):
        self.use_names("Ada Lovelase => Ada Lovelace\n")
        self.assertEqual("Ada Lovelace spoke. ada lovelases.\n",
                         podnote.apply_names("ada lovelase spoke. ada lovelases."))

    def test_drop_rule_and_builtin_drops_remove_bullets_only(self):
        self.use_names("!Acme\n")
        text = "- Acme sponsored this\n- a promo code here\n- real point\nAcme in prose stays"
        self.assertEqual("- real point\nAcme in prose stays\n", podnote.apply_names(text))

    def test_comment_and_host_lines_are_not_replacements(self):
        self.use_names("# Old => New\n@Show => Old\n")
        self.assertEqual("Old name\n", podnote.apply_names("Old name"))

    def test_builtin_drops_apply_without_a_names_file(self):
        with mock.patch.object(podnote, "NAMES_FILE", Path("/nonexistent/names.txt")):
            self.assertEqual("- Acme said this\n- Acme bans advertising to children\n",   # promotion goes, the subject stays
                             podnote.apply_names("- Sponsored by Acme\n- Acme said this\n- Acme bans advertising to children"))
            self.assertIn("never guess a surname", podnote.hosts_for("Any Show"))

    def test_hosts_match_a_show_fragment(self):
        self.use_names("@Starlight => Maya Lindqvist, Daniel Okafor\n")
        self.assertEqual("Maya Lindqvist, Daniel Okafor", podnote.hosts_for("Starlight: Two Astronomers on the News"))
        self.assertIn("never guess a surname", podnote.hosts_for("Other Show"))
        # no names.txt rule: the feed's host tags, else its author when that is a person
        brief = {"show_author": "Nadia Whitcombe", "show_owner": "Nadia Whitcombe"}
        self.assertEqual("Nadia Whitcombe", podnote.hosts_for("The Tech Morning Brief", brief))
        tagged = {"show_author": "Acme Media Ltd", "show_people": [{"name": "Ann Host", "role": "host"},
                                                                  {"name": "Gus Guest", "role": "guest"}]}
        self.assertEqual("Ann Host", podnote.hosts_for("Some Show", tagged))
        self.assertIn("never guess", podnote.hosts_for("The Daily Courier", {"show_author": "The Weekly Times"}))
        self.assertIn("never guess", podnote.hosts_for("Paradoxica", {"show_author": "Paradoxica"}))
        self.assertIn("never guess", podnote.hosts_for("Conversations", {"show_author": "The Open Press"}))
        self.assertIn("never guess", podnote.hosts_for("Quiet Hours", {"show_author": "The Weekly Times", "show_owner": "Lena Wells"}))
        self.assertEqual("Martin Grey", podnote.hosts_for("Outliers | Martin Grey", {"show_author": "Martin Grey"}))
        self.assertEqual("Omar Haddad", podnote.hosts_for("Signals with Omar Haddad", {"show_author": "Signal Media"}))
        self.assertEqual("Maya Lindqvist, Daniel Okafor", podnote.hosts_for("Starlight: Two Astronomers on the News", tagged))

    def test_inline_comments_are_ignored(self):
        # README documents "Ada Lovelase => Ada Lovelace      # fix a recurring ASR mishearing"
        self.use_names("Ada Lovelase => Ada Lovelace   # ASR mishearing\n"
                       "!Acme   # sponsor\n"
                       "@Starlight => Maya Lindqvist, Daniel Okafor  # hosts\n")
        self.assertEqual("Ada Lovelace spoke.\n- kept\n", podnote.apply_names("Ada Lovelase spoke.\n- Acme ad\n- kept"))
        self.assertEqual("Maya Lindqvist, Daniel Okafor", podnote.hosts_for("Starlight"))


class FakeModel:
    """Stands in for podnote.ask(): answers by prompt kind and records every call and prompt."""

    def __init__(self, **overrides: tuple[str, str]):
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}
        self.answers = {
            "section": ("**Topic:** the eclipse\n- Daniel Okafor argues the eclipse is fragile.\n"
                        "**References:**\n- [work] Cosmos (documentary) — cited by Okafor\n"
                        "- [quote] \"Nothing is more permanent than the temporary\" — Okafor, quoting a proverb", "stop"),
            "themes": ("**The eclipse**\nThe essence of the theme.\n- Fragile, Okafor argues.", "stop"),
            "summary": ("**Daniel Okafor** (host), **Ruth Calder** (guest)\n\nA clear paragraph.", "stop"),
            "tags": ('{"tags": ["science", "astronomy", "invented"], "missing": ["eclipses"]}', "stop"),
        }
        self.answers.update(overrides)

    def __call__(self, base_url, headers, prompt, max_tokens):
        kind = ("section" if prompt.startswith("You are taking reading notes") else
                "themes" if prompt.startswith("Below are sequential reading notes") else
                "tags" if prompt.startswith("Choose the subject tags") else "summary")
        self.calls.append(kind)
        self.prompts[kind] = prompt
        answer = self.answers[kind]
        if isinstance(answer, Exception):
            raise answer
        return answer


class SynthesiseTests(NamesFileTestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "podcast-1"
        (self.root / "source").mkdir(parents=True)
        (self.root / "asr").mkdir()
        (self.root / "source" / "resolved-episode-v1.json").write_text(json.dumps({
            "show": "Starlight: Two Astronomers on the News", "episode": "Forecasts", "published": "2026-10-07",
            "source_url": "https://example/x", "apple_episode_id": "1", "duration_seconds": 2400,
            "episode_description": "With Maya away, Daniel is joined by Ruth Calder. https://promo.example"}),
            encoding="utf-8")
        self.write_transcript("The eclipse forecast is fragile. Clouds remain.")
        self.use_names("@Starlight => Maya Lindqvist, Daniel Okafor\n")
        patcher = mock.patch.object(podnote, "MODEL", "model-a")
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_transcript(self, text: str) -> None:
        (self.root / "asr" / "transcript-v1.json").write_text(json.dumps({"chunks": [{"text": text}]}), encoding="utf-8")

    def run_with(self, model: FakeModel) -> tuple[str, list[str]]:
        with mock.patch.object(podnote, "ask", model), mock.patch.object(podnote, "log"):
            return podnote.synthesise(self.root, "http://model", {})

    def test_complete_note_layout(self):
        model = FakeModel()
        note, problems = self.run_with(model)
        self.assertEqual([], problems)
        self.assertTrue(note.startswith('---\norigin: agent\nmodel: model-a\ncreated: "'), note[:80])
        self.assertRegex(podnote.SKILL_VERSION, r"^\d+\.\d+\.\d+$")
        # header line, then the summary in a callout; the dropped "Participants:" label is restored
        self.assertIn("# Forecasts\n\n**Starlight: Two Astronomers on the News** · 2026-10-07 · ⏱ 40 min · "
                      "🎧 [Apple Podcasts](https://example/x)\n\n> [!abstract] Summary\n"
                      "> **Participants:** **Daniel Okafor** (host), **Ruth Calder** (guest)\n>\n> A clear paragraph.", note)
        # theme title as a heading, essence as a quote block, then the bullets
        self.assertIn("## Key themes\n\n### The eclipse\n> The essence of the theme.\n\n- Fragile, Okafor argues.", note)
        self.assertIn('hosts: ["Daniel Okafor"]\nguests: ["Ruth Calder"]\n', note)
        self.assertIn("## References\n\n### Works\n- Cosmos (documentary) — cited by Okafor", note)
        self.assertIn("### Quotes\n- \"Nothing is more permanent than the temporary\"", note)
        for gone in ("Facts and figures", "Points to treat cautiously", "quality:", "Assessment", "Main arguments", "## Summary"):
            self.assertNotIn(gone, note)

    def test_note_meets_the_metadata_check(self):
        meta = json.loads((self.root / "source" / "resolved-episode-v1.json").read_text())
        meta["published_utc"] = "2026-10-07T04:00:00"
        (self.root / "source" / "resolved-episode-v1.json").write_text(json.dumps(meta), encoding="utf-8")
        gemma = "gemma-4-26B-A4B-it-qat-4bit"                                 # a model the folder's catalogue knows
        with mock.patch.object(podnote, "MODEL", gemma):
            note, problems = self.run_with(FakeModel())
            shutil.rmtree(podnote.work_dir(self.root))                          # no cached answers for the next run
            gap, gap_problems = self.run_with(FakeModel(themes=("", "length")))
            shutil.rmtree(podnote.work_dir(self.root))
            with mock.patch.object(podnote, "ask", FakeModel()), mock.patch.object(podnote, "log"):
                remade, _ = podnote.synthesise(self.root, "http://model", {},
                                               {"created": "", "model": "Example-9B-8bit", "copies": ["old.md"]})
        front = note.split("---")[1]
        self.assertTrue(front.startswith(f'\norigin: agent\nmodel: {gemma}\ncreated: "'), front)
        self.assertRegex(front, r'updated: "\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d"')
        self.assertIn("tags: [science, astronomy]\n", front)                    # only the folder's tags
        self.assertIn('published: "2026-10-07T04:00:00"\n', front)
        self.assertIn("duration: 2400\n", front)                             # seconds
        for gone in ("type:", "area:", "scope:", "status:", "skill_version:", "synthesis_model:", "publisher:"):
            self.assertNotIn(gone, front)                                      # processing state stays outside
        record = json.loads((self.root / "note-record.json").read_text())
        self.assertEqual((gemma, ["eclipses"]), (record["composed_by"], record["tag_suggestions"]))
        self.assertNotIn("skill_version", record)                             # stamped only once the note is written
        self.assertEqual(["themes: length"], gap_problems)                    # a gap is a failure, never written
        # the model is the latest pass's; a start never recorded becomes now
        self.assertIn(f"model: {gemma}\n", remade)
        self.assertRegex(remade.split("---")[1], r'created: "\d{4}-')
        self.assertRegex(note.split("---")[1], r'created: "(.+)"\nupdated: "\1"')      # a new note: one instant
        # the check accepts all three (the last one as known repair debt)
        self.assertEqual([], podnote.validate(note))
        self.assertEqual([], podnote.validate(gap))
        self.assertEqual([], podnote.validate(remade))

    def test_a_failed_tag_call_leaves_the_note_untagged_not_incomplete(self):
        note, problems = self.run_with(FakeModel(tags=("{\"tags\": [\"science\", \"isr", "length")))
        self.assertEqual([], problems)
        self.assertNotIn("tags:", note.split("---")[1])

    def test_registry_tags_come_from_the_folder_tag_list(self):
        (podnote.ROOT / "_meta" / "tag-vocabulary.json").write_text(json.dumps(
            {"canonical_tags": ["ai", "science", podnote.SCRIPT_TAG], "proposed_aliases": {"AI": "ai"}}), encoding="utf-8")
        self.assertEqual((["ai", "science"], {"ai": "ai"}), podnote.tag_registry())   # the script marker is no subject

    def test_the_folder_standard_decides_else_the_basic_check(self):
        good = ('---\norigin: agent\nmodel: gemma-4-26B-A4B-it-qat-4bit\ncreated: "2026-09-01T10:00:00"\n'
                'updated: "2026-09-01T10:00:00"\nshow: "S"\nepisode: "E"\nsource_url: https://example.org/x\n'
                "apple_episode_id: '1'\nduration: 60\n---\n# E\n")
        other_model = good.replace("gemma-4-26B-A4B-it-qat-4bit", "x")
        undated = good.replace('created: "2026-09-01T10:00:00"\n', "")
        validator = podnote.ROOT / podnote.RULES["validator"]
        if validator.is_file():                                                # a folder standard under test
            self.assertEqual([], podnote.validate(good))
            self.assertIn("unverified_model:model", podnote.validate(other_model))
            self.assertEqual(["required_for_agent:created"], podnote.validate(undated))
            validator.unlink()
        self.assertIs(notecheck, podnote.note_checker())                       # never unchecked: the basic check
        self.assertEqual([], podnote.validate(good))
        self.assertEqual(["unverified_model:model"], podnote.validate(other_model))   # the folder's catalogue
        self.assertEqual(["required:created"], podnote.validate(undated))
        self.assertEqual(["required:show"], podnote.validate(good.replace('show: "S"\n', "")))
        self.assertEqual(["invalid_field:source_url"], podnote.validate(good.replace("https://example.org/x", "x")))
        self.assertEqual(["invalid_frontmatter"], podnote.validate(good.replace("show:", "episode:")))  # given twice
        shutil.rmtree(podnote.ROOT / "_meta")                                  # no standard, catalogue or tag list
        self.assertEqual([], podnote.validate(other_model.replace("duration:", "tags: [any-tag]\nduration:")))

    def test_provenance_comes_from_the_existing_notes(self):
        folder = podnote.ROOT / "Podcasts" / "Starlight"
        folder.mkdir(parents=True)
        note = folder / "old.md"
        with mock.patch.object(podnote, "EPISODES", Path("Podcasts")):
            self.assertEqual({"new": True, "copies": []}, podnote.provenance(self.root))
            note.write_text("---\napple_episode_id: '1'\nmodel: qwen-x\ncreated: '2026-09-01T10:00:00'\n"
                            "published: \"2026-08-31T05:00:00\"\n---\n")             # either quote style
            twin = folder / "older title.md"
            twin.write_text('---\napple_episode_id: "1"\ncreated: "2026-08-20T09:00:00"\n---\n')
            self.assertEqual({"created": "2026-08-20T09:00:00", "published": "2026-08-31T05:00:00",
                              "copies": [str(note), str(twin)]}, podnote.provenance(self.root))   # every copy found
            twin.unlink()
            note.write_text("---\napple_episode_id: '1'\nmodel: qwen-x\n---\n")
            self.assertEqual("", podnote.provenance(self.root)["created"])     # unknown: synthesise uses now
        self.assertEqual("2026-10-06T03:00:00", podnote.utc("2026-10-06T04:00:00+01:00"))
        self.assertEqual("", podnote.utc("2026-10-06"))                       # a date alone is not an instant

    def test_a_re_made_note_replaces_every_older_copy_and_a_failed_one_replaces_nothing(self):
        state = Path(self.root.parent) / "state"
        folder = podnote.ROOT / "Podcasts" / "Starlight"
        folder.mkdir(parents=True)
        old, older = folder / "2026-10-07 - Old title.md", folder / "2026-10-07 - Older title.md"
        for path in (old, older):
            path.write_text("---\napple_episode_id: '1'\n---\nold\n", encoding="utf-8")
        new = folder / "2026-10-07 - Forecasts.md"
        args = podnote.argparse.Namespace(url=None, job_dir=str(self.root), force=True, retranscribe=False)
        result = {"note": ("---\napple_episode_id: '1'\n---\nnew\n", [])}
        with mock.patch.object(podnote, "STATE", state), mock.patch.object(podnote, "EPISODES", Path("Podcasts")), \
                mock.patch.object(podnote, "register"), mock.patch.object(podnote, "note_path", lambda root: new), \
                mock.patch.object(podnote, "validate", lambda note, **kwargs: []), \
                mock.patch.object(podnote, "synthesise", lambda *a: result["note"]), \
                mock.patch.dict("os.environ", {"PODNOTE_BASE_URL": "http://model"}), mock.patch("builtins.print"):
            result["note"] = ("---\napple_episode_id: '1'\n---\npartial\n", ["themes: length"])
            self.assertEqual(1, podnote.run(args))                            # a failed part fails the note
            self.assertTrue(old.is_file() and older.is_file() and not new.exists())
            result["note"] = ("---\napple_episode_id: '1'\n---\nnew\n", [])
            self.assertEqual(0, podnote.run(args))
        self.assertTrue(new.is_file())
        self.assertFalse(old.exists() or older.exists())
        self.assertEqual(2, len(list((state / "superseded-notes").glob("*.md"))))
        record = json.loads((self.root / "note-record.json").read_text())
        self.assertEqual((podnote.SKILL_VERSION, str(new)), (record["skill_version"], record["note"]))

    def test_model_cache_from_another_transcript_is_set_aside(self):
        with mock.patch.object(podnote, "log"):
            work = podnote.fresh_work_dir(self.root)
            (work / "section-01.md").write_text("notes on the first transcript", encoding="utf-8")
            self.assertEqual(work, podnote.fresh_work_dir(self.root))             # same transcript: cache kept
            self.assertTrue((work / "section-01.md").is_file())
            self.write_transcript("A new transcript of the same episode.")
            work = podnote.fresh_work_dir(self.root)
        self.assertFalse((work / "section-01.md").exists())
        self.assertEqual(1, len(list(work.parent.glob(work.name + "-stale-*"))))

    def test_model_cache_from_another_part_size_is_set_aside(self):
        with mock.patch.object(podnote, "log"):
            work = podnote.fresh_work_dir(self.root)
            (work / "section-01.md").write_text("notes on an 1,800-word part", encoding="utf-8")
            with mock.patch.object(podnote, "SECTION_WORDS", 1200):          # same transcript, cut differently
                work = podnote.fresh_work_dir(self.root)
        self.assertFalse((work / "section-01.md").exists())
        self.assertEqual(1, len(list(work.parent.glob(work.name + "-stale-*"))))

    def test_self_introductions_need_a_greeting_or_sign_off(self):
        chunks = {"chunks": [{"text": "Hello, everybody, and welcome to Off the Fence with me, Tomas Brandt, and my "
                              "co-host Felix Moreau. " + "words " * 1200
                              + "I'm Team Newton, completely, on this one. " + "more " * 50
                              + "This has been Laser Focus. I am Lucas Berg, and we'll see you next week. "
                              + "Thanks for listening. I'm Iris Fleming."}]}
        self.assertEqual(["Tomas Brandt", "Felix Moreau", "Lucas Berg", "Iris Fleming"],
                         podnote.self_introductions(chunks, "Science Hour"))

    def test_asr_hint(self):
        hint = podnote.asr_hint({"show": "Starlight: Two Astronomers on the News", "episode": "Forecasts with Ruth Calder",
                                 "episode_description": "With Maya away, Daniel is joined by Ruth Calder. https://promo.example"})
        self.assertTrue(hint.startswith("Podcast: Starlight: Two Astronomers on the News. Hosts: Maya Lindqvist, Daniel Okafor. "
                                        "Names: Ruth Calder. Episode: Forecasts with Ruth Calder. With Maya away"), hint)
        self.assertNotIn("http", hint)

    def test_summary_prompt_asks_for_a_quick_summary_and_substance_only(self):
        self.assertIn("the quick summary: one plain sentence", podnote.SUMMARY_PROMPT)
        self.assertNotIn("entiment", podnote.SUMMARY_PROMPT)
        self.assertNotIn("worth hearing", podnote.SUMMARY_PROMPT)

    def test_no_highlights_anywhere(self):
        callout = podnote.summary_callout("**Participants:** **A** (host)\n\nIt turns on ==social contracts== and deals.")
        self.assertEqual("> [!abstract] Summary\n> **Participants:** **A** (host)\n>\n> It turns on social contracts and deals.", callout)
        themes = podnote.render_themes("**Western models**\nThey fail on ==contact with reality==.\n\n- **Point** — a ==bullet==.")
        self.assertEqual("### Western models\n> They fail on contact with reality.\n\n- **Point** — a bullet.", themes)
        for prompt in (podnote.SUMMARY_PROMPT, podnote.THEMES_PROMPT):
            self.assertNotIn("==", prompt)
            self.assertNotIn("highlight", prompt.lower())

    def test_summary_prompt_gets_description_and_only_hosts_present(self):
        model = FakeModel()
        self.run_with(model)
        self.assertIn("Daniel is joined by Ruth Calder", model.prompts["summary"])
        self.assertIn("Hosts taking part: Daniel Okafor\n", model.prompts["summary"])   # Maya Lindqvist is away
        self.assertNotIn("http", model.prompts["section"])

    def test_all_hosts_are_passed_when_the_notes_name_none(self):
        model = FakeModel(section=("**Topic:** t\n- A point with no names.", "stop"),
                          themes=("**T**\nEssence.\n- point", "stop"))
        self.run_with(model)
        self.assertIn("Hosts taking part: not named in the notes (the show's hosts: Maya Lindqvist, Daniel Okafor)\n",
                      model.prompts["summary"])

    def test_themes_prompt_is_sized_to_the_episode(self):
        model = FakeModel()
        self.run_with(model)
        self.assertIn("(40 minutes, 1 parts)", model.prompts["themes"])
        self.assertIn("Aim for about 3 themes", model.prompts["themes"])

    def test_failed_themes_are_a_problem_with_no_stand_in(self):
        note, problems = self.run_with(FakeModel(themes=("half a theme", "length")))
        self.assertEqual(["themes: length"], problems)          # run() then writes nothing; the old note stays
        self.assertNotIn("### Part 1", note)                    # no part notes passed off as themes
        self.assertNotIn("Incomplete", note)

    def test_skipped_part_is_left_out(self):
        sentence = " ".join(["word"] * 99) + "."
        self.write_transcript(" ".join([sentence] * 30))           # 3,000 words: two parts
        model = FakeModel()
        answers = iter([("SKIP", "stop"), model.answers["section"]])
        with mock.patch.object(podnote, "ask", lambda *a: next(answers) if a[2].startswith("You are") else model(*a)), \
                mock.patch.object(podnote, "log"):
            note, problems = podnote.synthesise(self.root, "http://model", {})
        self.assertEqual([], problems)
        self.assertEqual(1, note.count("Cosmos"))

    def test_unreachable_server_stops_without_a_note(self):
        import requests
        with self.assertRaises(podnote.Stop):
            self.run_with(FakeModel(section=requests.exceptions.ConnectionError("refused")))

    def test_rerun_reuses_cache_for_the_same_model_only(self):
        first = FakeModel()
        self.run_with(first)
        self.assertEqual(["section", "themes", "summary", "tags"], first.calls)
        again = FakeModel()
        self.run_with(again)
        self.assertEqual([], again.calls)
        with mock.patch.object(podnote, "MODEL", "model-b"):
            other = FakeModel(summary=("**Participants:** **Daniel Okafor** (host)\n\nModel B.", "stop"))
            note, _ = self.run_with(other)
        self.assertEqual(["section", "themes", "summary", "tags"], other.calls)
        self.assertIn("model: model-b", note)
        self.assertIn("Model B.", note)


class AskTests(unittest.TestCase):

    def test_request_uses_gemma_sampling_without_penalties_or_thinking(self):
        response = mock.Mock()
        response.json.return_value = {"choices": [{"message": {"content": "```markdown\nnotes\n```"},
                                                   "finish_reason": "stop"}]}
        with mock.patch.object(podnote.requests, "post", return_value=response) as post:
            self.assertEqual(("notes", "stop"), podnote.ask("http://model", {}, "prompt", 900))
        body = post.call_args.kwargs["json"]
        self.assertEqual((1.0, 0.95, 64), (body["temperature"], body["top_p"], body["top_k"]))
        self.assertNotIn("presence_penalty", body)
        self.assertNotIn("frequency_penalty", body)
        self.assertEqual({"enable_thinking": False}, body["chat_template_kwargs"])
        self.assertEqual(900, body["max_tokens"])


class StampAndPathTests(unittest.TestCase):

    def test_listening_time(self):
        self.assertEqual("", podnote.listening_time(0))
        self.assertEqual("29 min", podnote.listening_time(29 * 60 + 10))
        self.assertEqual("1h 17m", podnote.listening_time(77 * 60))

    def test_stamp(self):
        self.assertEqual("0:00:00", podnote.stamp(0))
        self.assertEqual("1:02:03", podnote.stamp(3723.9))

    def test_note_path_is_dated_and_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source").mkdir()
            (root / "source" / "resolved-episode-v1.json").write_text(json.dumps({
                "published": "2026-10-07", "show": "Show: Name", "episode": 'A/B "test"? #1'}), encoding="utf-8")
            with mock.patch.object(podnote, "ROOT", Path("/notes")), \
                    mock.patch.object(podnote, "EPISODES", Path("Episodes")):
                # the show's own folder (title cut at ": " when the registry does not know it), date and title
                self.assertEqual(Path("/notes/Episodes/Show/2026-10-07 - AB test 1.md"), podnote.note_path(root))


if __name__ == "__main__":
    unittest.main()


class RegistryRoleTests(unittest.TestCase):

    def test_registry_hosts_are_labelled_hosts_and_a_single_host_is_named(self):
        summary = "**Participants:** **Noam Weissman** (guest), **Ann Lee** (guest, historian)\n\nA paragraph."
        self.assertTrue(podnote.registry_roles(summary, ["Noam Weissman"]).startswith(
            "**Participants:** **Noam Weissman** (host), **Ann Lee** (guest, historian)"))
        text = "The host argues X. The host's view is Y. A host country votes.\n> \"the host said so\""
        self.assertEqual("Noam Weissman argues X. Noam Weissman's view is Y. A host country votes.\n> \"the host said so\"",
                         podnote.name_the_host(text, ["Noam Weissman"]))
        self.assertEqual(text, podnote.name_the_host(text, ["A", "B"]))      # two hosts: no guessing


class AudioLifecycleTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "podcast-1"
        (self.root / "source").mkdir(parents=True)
        (self.root / "asr" / "windows").mkdir(parents=True)

    def test_audio_is_dropped_only_once_the_transcript_exists(self):
        (self.root / "source" / "episode-audio").write_bytes(b"a" * 300)
        (self.root / "asr" / "windows" / "window-0001.wav").write_bytes(b"w" * 200)
        self.assertEqual(0, podnote.drop_audio(self.root))              # mid-transcription: still needed
        (self.root / "asr" / "transcript-v1.json").write_text("{}")
        self.assertEqual(500, podnote.drop_audio(self.root))
        self.assertTrue((self.root / "asr" / "transcript-v1.json").is_file())
        self.assertFalse((self.root / "source" / "episode-audio").exists())

    def test_an_expired_audio_link_is_resolved_again_once(self):
        import audio_acquisition
        from types import SimpleNamespace
        metadata_path = self.root / "source" / "resolved-episode-v1.json"
        metadata = {"audio_url": "https://cdn.example/old.mp3?sig=expired"}
        tried = []

        def acquire(url, target):
            tried.append(url)
            if "expired" in url:
                raise audio_acquisition.AudioError("Audio server returned HTTP 403")
        fresh = SimpleNamespace(enclosure_url="https://cdn.example/new.mp3?sig=fresh")
        with mock.patch.object(audio_acquisition, "acquire_audio", acquire), \
                mock.patch("podfetch.refresh_enclosure_if_needed", return_value=fresh), mock.patch.object(podnote, "log"):
            podnote.fetch_audio("https://podcasts.apple.com/us/podcast/id1?i=2", metadata, metadata_path,
                                self.root / "source" / "episode-audio")
        self.assertEqual(["https://cdn.example/old.mp3?sig=expired", "https://cdn.example/new.mp3?sig=fresh"], tried)
        self.assertIn("sig=fresh", metadata_path.read_text())

    def test_other_download_errors_are_not_retried(self):
        import audio_acquisition
        with mock.patch.object(audio_acquisition, "acquire_audio",
                               side_effect=audio_acquisition.AudioTransientError("Audio server returned HTTP 503")), \
                mock.patch("podfetch.refresh_enclosure_if_needed") as refresh, self.assertRaises(audio_acquisition.AudioError):
            podnote.fetch_audio("u", {"audio_url": "https://cdn.example/a.mp3"}, self.root / "m.json", self.root / "a")
        refresh.assert_not_called()


class ModelOutputDriftTests(unittest.TestCase):
    """The model's part notes vary in small ways; the references must come out the same."""

    def test_reference_label_and_bullet_variants(self):
        expected = ["[work] Cosmos (documentary) — cited by Okafor", '[quote] "Nothing is lost" — Okafor']
        for label in ("**References:**", "References:", "### References", "**References**:", "References"):
            for bullet in ("- ", "* ", "  - "):
                notes = (f"**Topic:** eclipses\n- A point.\n\n{label}\n\n{bullet}{expected[0]}\n{bullet}{expected[1]}\n")
                points, entries = podnote.split_references(notes)
                self.assertEqual(expected, entries, (label, bullet))
                self.assertNotIn("Cosmos", points)

    def test_an_inline_first_entry_and_none_are_handled(self):
        _, entries = podnote.split_references("- A point.\n**References:** [work] Cosmos (documentary) — on stars\n")
        self.assertEqual(["[work] Cosmos (documentary) — on stars"], entries)
        self.assertEqual([], podnote.split_references("- A point.\n**References:**\n- None.\n")[1])

    def test_works_from_adverts_are_left_out(self):
        section = podnote.references_section([
            "[work] Harbour Nights — a podcast described in an advertisement",
            "[work] Tide Tables — sponsored segment",
            "[work] The Sponsor — a novel cited by Okafor",                     # the word in a title is fine
            "[work] Mad Men (TV series) — cited on the history of advertising"])  # a subject, not an advert
        self.assertNotIn("Harbour Nights", section)
        self.assertNotIn("Tide Tables", section)
        self.assertIn("The Sponsor", section)
        self.assertIn("Mad Men", section)

    def test_tags_in_any_case_are_read(self):
        section = podnote.references_section(["[Work] Cosmos (documentary) — on stars", "[IDEA] Occam's razor — on simplicity",
                                              "an untagged bullet"])
        self.assertIn("Cosmos", section)
        self.assertIn("Occam's razor", section)
        self.assertNotIn("untagged", section)
