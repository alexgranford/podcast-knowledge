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
