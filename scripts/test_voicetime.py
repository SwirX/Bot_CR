"""Hermetic checks for the voicetime leaderboard ranking + formatting.

Run:  .venv-local/bin/python scripts/test_voicetime.py
No network: rank_voice and fmt_seconds are pure module functions.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.stats import fmt_seconds, rank_voice  # noqa: E402


class FmtSecondsTests(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(fmt_seconds(0), "00h 00m 00s")

    def test_hours_minutes_seconds(self):
        self.assertEqual(fmt_seconds(3661), "01h 01m 01s")

    def test_sub_second_floor(self):
        self.assertEqual(fmt_seconds(59.9), "00h 00m 59s")


class RankVoiceTests(unittest.TestCase):
    def test_sorts_descending(self):
        rows = [("a", 10), ("b", 500), ("c", 42)]
        self.assertEqual([name for name, _ in rank_voice(rows)],
                         ["b", "c", "a"])

    def test_drops_zero_and_negative(self):
        rows = [("zero", 0), ("neg", -3), ("real", 60)]
        self.assertEqual([name for name, _ in rank_voice(rows)], ["real"])

    def test_cap_at_25(self):
        rows = [(f"m{i}", i + 1) for i in range(40)]
        self.assertEqual(len(rank_voice(rows, 25)), 25)

    def test_limit_min_1(self):
        rows = [("a", 5)]
        self.assertEqual(len(rank_voice(rows, 0)), 1)

    def test_limit_respected(self):
        rows = [("a", 5), ("b", 4), ("c", 3)]
        self.assertEqual(len(rank_voice(rows, 2)), 2)

    def test_empty(self):
        self.assertEqual(rank_voice([]), [])


if __name__ == "__main__":
    unittest.main()