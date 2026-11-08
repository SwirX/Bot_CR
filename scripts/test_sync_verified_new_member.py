import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("APPWRITE_API_KEY", "y")

from scripts.sync_verified_new_member import missing_new_member  # noqa: E402


class VerifiedRoleSweepTests(unittest.TestCase):
    def test_only_verified_humans_missing_the_role_are_selected(self):
        verified, new = object(), object()
        members = [SimpleNamespace(id=1, bot=False, roles=[verified]),
                   SimpleNamespace(id=2, bot=False, roles=[verified, new]),
                   SimpleNamespace(id=3, bot=False, roles=[]),
                   SimpleNamespace(id=4, bot=True, roles=[verified])]
        self.assertEqual([m.id for m in missing_new_member(members, verified, new)], [1])
