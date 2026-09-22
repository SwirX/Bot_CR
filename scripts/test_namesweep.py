"""Hermetic checks for the /namesweep planner and cursive helpers.

Run:  .venv-local/bin/python scripts/test_namesweep.py
No network: plan_namesweep, has_cursive and decursive are pure functions; fake
members stand in for the Discord model.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.names import decursive, has_cursive  # noqa: E402
from cogs.onboarding import to_cursive, plan_namesweep  # noqa: E402


class _Member:
    def __init__(self, uid, nick=""):
        self.id = uid
        self.nick = nick


class _Rec:
    def __init__(self, uid, *, real_name="", birthday=""):
        self.uid = str(uid)
        self.real_name = real_name
        self.birthday = birthday

    def get(self, key, default=None):
        return getattr(self, key, default)


class CursiveHelpersTests(unittest.TestCase):
    def test_has_cursive_detects_bold_script(self):
        self.assertTrue(has_cursive("𝓕𝓪𝓽𝓲𝓶𝓪 𝓑𝓸𝓾𝔃𝓪𝓻𝓫𝓲𝓪"))
        self.assertFalse(has_cursive("Fatima Bouzarbia"))
        self.assertFalse(has_cursive(""))
        self.assertFalse(has_cursive(None))

    def test_roundtrip(self):
        name = "Yasser El Joundi"
        self.assertEqual(decursive(to_cursive(name)), name)

    def test_arabic_passthrough(self):
        cursive = to_cursive("حمدي أحمد")
        self.assertEqual(decursive(cursive), "حمدي أحمد")


class PlanNamesweepTests(unittest.TestCase):
    def test_backfills_cursive_without_stored_name(self):
        member = _Member(1, to_cursive("Fatima Bouzarbia"))
        backfill, skipped, name_dm, birthday_dm = plan_namesweep(
            [member], {"1": _Rec(1)})
        self.assertEqual([(m.id, plain) for m, plain in backfill],
                         [(1, "Fatima Bouzarbia")])
        self.assertEqual(skipped, [])
        self.assertEqual(name_dm, [])
        self.assertIn(member, birthday_dm)

    def test_skips_members_with_stored_real_name(self):
        member = _Member(2, to_cursive("Ahmed"))
        rec = _Rec(2, real_name="Ahmed", birthday="03-14")
        backfill, skipped, name_dm, birthday_dm = plan_namesweep([member], {"2": rec})
        self.assertEqual(backfill, [])
        self.assertEqual([m.id for m in skipped], [2])
        self.assertEqual(name_dm, [])
        self.assertEqual(birthday_dm, [])  # has birthday

    def test_plain_nick_without_name_gets_nudged(self):
        member = _Member(3, "Med")
        backfill, skipped, name_dm, birthday_dm = plan_namesweep(
            [member], {"3": _Rec(3)})
        self.assertEqual(backfill, [])
        self.assertIn(member, name_dm)
        self.assertIn(member, birthday_dm)

    def test_no_record_means_missing_everything(self):
        member = _Member(4, None)
        backfill, skipped, name_dm, birthday_dm = plan_namesweep([member], {})
        self.assertEqual(backfill, [])
        self.assertIn(member, name_dm)
        self.assertIn(member, birthday_dm)

    def test_empty_member_list(self):
        self.assertEqual(plan_namesweep([], {}),
                         ([], [], [], []))


if __name__ == "__main__":
    unittest.main()