#!/usr/bin/env python3
"""The show registry: hosts that keep themselves up to date from evidence, fixes, advertisers, overview."""

from __future__ import annotations

import json
import sys
import unittest
from unittest import mock
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import podnote
import podshows

SHOW = "1000000001"


class RegistryTests(unittest.TestCase):

    def setUp(self):
        self.folder = podshows.shows_root() / "The Daily Courier"
        self.folder.mkdir(parents=True)
        with podshows.editing() as registry:
            podshows.ensure_show(registry, SHOW, "The Daily Courier", "gb", {"show_author": "The Weekly Times"})
        self.day = date(2026, 10, 7)

    def note(self, days_ago: int, hosts: list[str], log: str = "") -> None:
        published = (self.day - timedelta(days=days_ago)).isoformat()
        (self.folder / f"{published} - Episode.md").write_text(
            f"---\ntype: podcast\npublished: {published}\napple_episode_id: '{days_ago}'\nhosts: {json.dumps(hosts)}\n---\n"
            f"# Episode\n\n> [!info]- Verification log\n{log}", encoding="utf-8")

    def refreshed(self, **feed) -> dict:
        with podshows.editing() as registry:
            podshows.refresh(registry, SHOW, feed or None)
            return registry[SHOW]

    def roles(self, show: dict) -> dict:
        return {h["name"]: h["role"] for h in show["hosts"]}

    def test_regular_hosts_and_a_weekly_visiting_cohost(self):
        for day in range(20):                       # a daily show; a visiting co-host once a week
            self.note(day, ["Clara Benton", "Hugo Marsh"] + (["Jacob Rees-Mogg"] if day % 7 == 0 else []))
        show = self.refreshed()
        self.assertEqual({"Clara Benton": "regular", "Hugo Marsh": "regular", "Jacob Rees-Mogg": "co-host"},
                         self.roles(show))
        self.assertEqual("The Weekly Times", show["publisher"])
        regular, cohosts = podshows.hosts_on(show, "2026-10-07")
        self.assertEqual((["Clara Benton", "Hugo Marsh"], ["Jacob Rees-Mogg"]), (regular, cohosts))

    def test_a_week_off_retires_nobody_but_a_long_absence_does(self):
        for day in range(70, 100):
            self.note(day, ["Clara Benton", "Hugo Marsh"] + (["Jacob Rees-Mogg"] if day % 7 == 0 else []))
        for day in range(10, 70):                   # Tim has left; Jacob still visits weekly
            self.note(day, ["Clara Benton"] + (["Jacob Rees-Mogg"] if day % 7 == 0 else []))
        self.note(0, ["Clara Benton"])          # Jacob's normal gap: no retirement
        show = self.refreshed()
        self.assertEqual("former", self.roles(show)["Hugo Marsh"])
        tim = next(h for h in show["hosts"] if h["name"] == "Hugo Marsh")
        self.assertEqual("2026-07-29", tim["until"])                           # his last episode, 70 days back
        self.assertEqual("co-host", self.roles(show)["Jacob Rees-Mogg"])
        self.assertIn("Hugo Marsh", podshows.hosts_on(show, "2026-07-01")[0])      # still host of his episodes
        self.assertNotIn("Hugo Marsh", podshows.hosts_on(show, "2026-10-01")[0])

    def test_strong_sources_add_at_once_and_one_episode_is_not_enough(self):
        self.note(0, ["Someone Once"])
        show = self.refreshed(show_people=[{"name": "Ann Host", "role": "host"}], show_author="The Weekly Times")
        self.assertEqual({"Ann Host": "regular"}, self.roles(show))
        for day in (1, 2):
            self.note(day, ["Someone Once"])
        self.assertIn("Someone Once", self.roles(self.refreshed()))                # third episode: evidence

    def test_the_users_word_is_locked_and_wins(self):
        with podshows.editing() as registry:
            podshows.add_host(registry[SHOW], "Nora Pike", "user", locked=True)
            podshows.add_host(registry[SHOW], "Lena Wells", "user", role="excluded", locked=True)
        for day in range(30):                       # absent from every recent note, never retired
            self.note(day, ["Tom Hart"])
        show = self.refreshed(show_owner="Lena Wells")
        self.assertEqual("regular", self.roles(show)["Nora Pike"])
        self.assertEqual("excluded", self.roles(show)["Lena Wells"])
        self.assertNotIn("Lena Wells", podshows.hosts_on(show, "2026-10-07")[0])

    def test_name_fixes_need_two_episodes_and_advertisers_expire(self):
        self.note(3, ["Clara Benton"], "> - Alistair Burnet → Alastair Burnet (name)\n")
        self.assertEqual({}, self.refreshed()["fixes"])
        self.note(2, ["Clara Benton"], "> - Alistair Burnet → Alastair Burnet (name)\n")
        self.assertEqual({"Alistair Burnet": "Alastair Burnet"}, self.refreshed()["fixes"])
        with podshows.editing() as registry:
            podshows.refresh(registry, SHOW, None, ["Northwind"], "2026-05-01")
            podshows.refresh(registry, SHOW, None, ["Harbourline"], "2026-10-01")
            self.assertEqual({"Harbourline": "2026-10-01"}, registry[SHOW]["advertisers"])

    def test_hosts_are_read_in_either_list_style(self):
        flow = '---\npublished: "2026-10-07T04:00:00"\nhosts: ["A B", "C D"]\napple_episode_id: \'1\'\n---\n'
        block = "---\npublished: \"2026-10-07T04:00:00\"\nhosts:\n- A B\n- 'C D'\nguests:\n- E F\napple_episode_id: '1'\n---\n"
        self.assertEqual(["A B", "C D"], podshows.front(flow)["hosts"])
        self.assertEqual(["A B", "C D"], podshows.front(block)["hosts"])

    def test_folder_names_overview_and_digest(self):
        self.assertEqual("The Tech Morning Brief", podshows.folder_name("The Tech Morning Brief: Technology News"))
        self.assertEqual("Outliers", podshows.folder_name("Outliers | Martin Grey"))
        self.assertEqual("Make-Do with Ann, Bo, Cy & Dee",
                         podshows.folder_name("Make-Do with Ann, Bo, Cy & Dee"))
        self.assertEqual(SHOW, podshows.find(podshows.load(), "daily courier"))
        self.assertEqual(SHOW, podshows.find(podshows.load(), f"https://podcasts.apple.com/gb/podcast/x/id{SHOW}"))
        self.note(0, ["Clara Benton"])
        path = podshows.render()
        front, overview = path.read_text(encoding="utf-8").split("---")[1], path.read_text(encoding="utf-8")
        self.assertIn("| [The Daily Courier](https://podcasts.apple.com/gb/podcast/id1000000001) |  | The Weekly Times |", overview)
        self.assertIn("origin: agent\n", front)
        self.assertRegex(front, r'created: "\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d"')   # made now: a known start
        self.assertIn(f"tags: [{podnote.SCRIPT_TAG}]\n", front)                        # script-made: marker, no model
        self.assertNotIn("model:", front)
        # an overview whose start was never recorded keeps it unknown, and keeps the folder's own tags
        path.write_text(f"---\norigin: agent\nupdated: \"2026-10-08T19:04:24\"\ntags:\n- overview\n- {podnote.SCRIPT_TAG}\n---\n"
                        + overview.split("---", 2)[2], encoding="utf-8")
        front = podshows.render().read_text(encoding="utf-8").split("---")[1]
        self.assertRegex(front, r'created: "\d{4}-')                          # unknown start: now
        self.assertIn(f"tags: [overview, {podnote.SCRIPT_TAG}]\n", front)
        again = path.read_text(encoding="utf-8")
        podshows.render()                                                      # nothing new: `updated` stays
        self.assertEqual(again, path.read_text(encoding="utf-8"))
        self.assertIn("new show (The Daily Courier)", podshows.digest())
        self.assertEqual("", podshows.digest())                               # nothing new since


if __name__ == "__main__":
    unittest.main()


class NamingTests(unittest.TestCase):

    def test_folder_names_stop_at_the_first_separator(self):
        self.assertEqual("Harbour Live", podshows.folder_name(
            "Harbour Live - Tide tables, shipping forecasts (sea areas), lighthouses"))
        self.assertEqual("Make-Do", podshows.folder_name("Make-Do"))
        self.assertLessEqual(len(podshows.folder_name("word " * 30)), 60)

    def test_the_us_catalogue_is_asked_first(self):
        import podfetch
        self.assertEqual(["us", "gb"], podfetch.storefronts("gb"))
        self.assertEqual(["us"], podfetch.storefronts("US"))
