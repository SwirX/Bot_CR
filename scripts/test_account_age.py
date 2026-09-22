"""Hermetic checks for the account-age helper and leaderboard ranking.

Run:  .venv-local/bin/python scripts/test_account_age.py
No network: age_summary and oldest_members are pure functions exercised with
fixed clocks and fake members.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.fun import age_summary, oldest_members  # noqa: E402


NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


class AgeSummaryTests(unittest.TestCase):
    def age(self, days_ago):
        return age_summary(NOW - timedelta(days=days_ago), now=NOW)

    def test_multi_year_breakdown(self):
        s = self.age(365 * 5 + 30 * 2 + 3)  # 5y 2m 3d
        self.assertEqual((s.years, s.months, s.days), (5, 2, 3))
        self.assertEqual(s.compact, "5y 2m 3d")
        self.assertEqual(s.sentence, "5 years, 2 months")  # days dropped for readability

    def test_sub_year_uses_days(self):
        s = self.age(100)  # 3m 10d
        self.assertEqual((s.years, s.months, s.days), (0, 3, 10))
        self.assertEqual(s.compact, "3m 10d")
        self.assertEqual(s.sentence, "3 months, 10 days")

    def test_tag_tiers(self):
        self.assertIn("ancient", self.age(365 * 10).tag)
        self.assertIn("veteran", self.age(365 * 7).tag)
        self.assertIn("seasoned", self.age(365 * 4).tag)
        self.assertIn("real one", self.age(365 * 2).tag)
        self.assertIn("fresh", self.age(100).tag)

    def test_naive_created_at_assumed_utc(self):
        s = age_summary(datetime(2020, 1, 1), now=NOW)  # ~6.7 years
        self.assertEqual(s.years, 6)

    def test_future_creation_degrades_gracefully(self):
        s = age_summary(NOW + timedelta(days=1), now=NOW)
        self.assertEqual(s.compact, "0s")
        self.assertEqual(s.years, 0)


class _Member:
    def __init__(self, name, created_at, bot=False):
        self.name = name
        self.display_name = name
        self.created_at = created_at
        self.bot = bot


class OldestMembersTests(unittest.TestCase):
    def test_sorts_oldest_first_and_skips_bots(self):
        older = _Member("older", NOW - timedelta(days=3650))
        middle = _Member("middle", NOW - timedelta(days=365))
        newer = _Member("newer", NOW - timedelta(days=30))
        bot = _Member("bot", NOW - timedelta(days=10000), bot=True)
        ranked = oldest_members([newer, bot, older, middle])
        self.assertEqual([m.name for m in ranked], ["older", "middle", "newer"])

    def test_missing_created_at_excluded(self):
        no_date = _Member("ghost", None)
        ranked = oldest_members([no_date, _Member("real", NOW - timedelta(days=100))])
        self.assertEqual([m.name for m in ranked], ["real"])

    def test_limit_is_clamped(self):
        members = [_Member(f"m{i}", NOW - timedelta(days=3650 - i)) for i in range(30)]
        self.assertEqual(len(oldest_members(members, 50)), 25)
        self.assertEqual(len(oldest_members(members, 3)), 3)

    def test_empty_input(self):
        self.assertEqual(oldest_members([]), [])


if __name__ == "__main__":
    unittest.main()