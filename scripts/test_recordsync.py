#!/usr/bin/env python3
"""Unit tests for the record-sync planning helpers (cogs/recordsync.py).

Covers the pure classification/backfill logic — no Discord gateway, no
Appwrite. Run directly: `python scripts/test_recordsync.py`
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("APPWRITE_API_KEY", "y")
os.environ.setdefault("APPWRITE_PROJECT_ID", "robotics-ops")
os.environ.setdefault("APPWRITE_DATABASE_ID", "robotics_hub")

from cogs.recordsync import (  # noqa: E402
    discord_payload, plan_collection, plan_sync, primary_role_id,
)


class FakeRole:
    def __init__(self, role_id, name="role", position=1):
        self.id, self.name, self.position = role_id, name, position


class FakeAvatar:
    def __init__(self, url):
        self.url = url


class FakeMember:
    def __init__(self, mid, *, name="user", nick=None, guild_avatar=None,
                 avatar=None, roles=(), joined_at=None, bot=False):
        self.id = mid
        self.name = name
        self.nick = nick
        self.bot = bot
        self.roles = list(roles)
        self.joined_at = joined_at
        self.guild_avatar = guild_avatar
        self.avatar = avatar

    @property
    def display_name(self):
        return self.nick or self.name


def _records(**kw):
    return kw


class PayloadTests(unittest.TestCase):
    def test_payload_carries_all_identity_fields(self):
        m = FakeMember("1", name="swirx", nick="Swir",
                       avatar=FakeAvatar("https://cdn/a.png"),
                       roles=[FakeRole(10, "member", 1),
                              FakeRole(20, "archon", 5)],
                       joined_at=datetime(2020, 1, 2, 3, 4, tzinfo=timezone.utc))
        p = discord_payload(m, default_role_id=99)
        self.assertEqual(p["username"], "swirx")
        self.assertEqual(p["display_name"], "Swir")
        self.assertEqual(p["avatar_url"], "https://cdn/a.png")
        self.assertEqual(p["role_id"], "20")  # highest position
        self.assertTrue(p["joined_at"].startswith("2020-01-02T03:04"))

    def test_role_id_ignores_default_role(self):
        m = FakeMember("1", roles=[FakeRole(1, "@everyone", 0),
                                   FakeRole(2, "member", 3)])
        self.assertEqual(primary_role_id(m, default_role_id=1), "2")

    def test_role_id_blank_when_only_everyone(self):
        m = FakeMember("1", roles=[FakeRole(1, "@everyone", 0)])
        self.assertEqual(primary_role_id(m, default_role_id=1), "")

    def test_avatar_prefers_guild_avatar(self):
        m = FakeMember("1", guild_avatar=FakeAvatar("g.png"),
                       avatar=FakeAvatar("u.png"))
        self.assertEqual(discord_payload(m)["avatar_url"], "g.png")

    def test_avatar_blank_when_none(self):
        self.assertEqual(discord_payload(FakeMember("1"))["avatar_url"], "")


class PlanSyncTests(unittest.TestCase):
    def test_only_missing_fills_nulls_and_skips_filled(self):
        m = FakeMember("1", name="a", nick="A", avatar=FakeAvatar("x.png"),
                       roles=[FakeRole(7, "r", 1)])
        # record: username filled, display_name + avatar blank
        rec = {"user_id": "1", "username": "a", "display_name": "",
               "avatar_url": "", "joined_at": None, "role_id": ""}
        updates, unchanged, _ = plan_sync([m], {"1": rec}, only_missing=True,
                                        default_role_id=0)
        self.assertEqual(len(updates), 1)
        payload = updates[0][1]
        # username already set -> not included; blanks get filled
        self.assertNotIn("username", payload)
        self.assertIn("display_name", payload)
        self.assertIn("avatar_url", payload)
        self.assertIn("role_id", payload)

    def test_force_refreshes_every_field(self):
        m = FakeMember("1", name="newname", nick="New")
        rec = {"user_id": "1", "username": "old", "display_name": "Old",
               "avatar_url": "old.png", "joined_at": "2020-01-01T00:00:00+00:00",
               "role_id": "99"}
        updates, unchanged, _ = plan_sync([m], {"1": rec}, only_missing=False)
        payload = updates[0][1]
        self.assertEqual(payload["username"], "newname")
        self.assertEqual(payload["display_name"], "New")

    def test_unchanged_member_produces_no_update(self):
        m = FakeMember("1", name="a", nick="A")
        rec = {"user_id": "1", "username": "a", "display_name": "A",
               "avatar_url": "", "joined_at": None, "role_id": ""}
        # nothing to fill (avatar/join/role all blank on the member side)
        m.roles = []
        m.avatar = None
        updates, unchanged, _ = plan_sync([m], {"1": rec}, only_missing=True)
        self.assertEqual(updates, [])
        self.assertEqual(unchanged, 1)

    def test_missing_row_is_created(self):
        m = FakeMember("1")
        updates, _, no_row = plan_sync([m], {}, only_missing=True)
        self.assertEqual(no_row, 1)
        self.assertEqual(len(updates), 1)

    def test_bots_are_skipped(self):
        b = FakeMember("1", bot=True)
        updates, unchanged, no_row = plan_sync([b], {}, only_missing=True)
        self.assertEqual(updates, [])
        self.assertEqual((no_row, unchanged), (0, 0))


class PlanCollectionTests(unittest.TestCase):
    def test_splits_missing_name_and_birthday(self):
        m1 = FakeMember("1")  # missing both
        m2 = FakeMember("2")  # has both
        m3 = FakeMember("3")  # name only
        recs = {
            "2": {"real_name": "X", "birthday": "01-01"},
            "3": {"real_name": "Y", "birthday": ""},
        }
        needs_name, needs_bday = plan_collection([m1, m2, m3], recs)
        # m1 missing both; m3 has a name but no birthday; m2 complete.
        self.assertEqual({m.id for m in needs_name}, {"1"})
        self.assertEqual({m.id for m in needs_bday}, {"1", "3"})

    def test_bots_skipped(self):
        b = FakeMember("1", bot=True)
        needs_name, needs_bday = plan_collection([b], {})
        self.assertEqual(needs_name, [])
        self.assertEqual(needs_bday, [])


if __name__ == "__main__":
    unittest.main()