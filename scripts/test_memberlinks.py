"""Live contract checks for the club-member ↔ Discord link layer + level save.

Run:  .venv-local/bin/python scripts/test_memberlinks.py
Touches the real robotics_hub, but ONLY disposable ``tst-…`` row ids; every row
created here is deleted at the end, leaving the hub exactly as it was.

Covers:
  * member_discord_links create / re-point / unlink, FK survival on re-patch
    (TablesDB 1.9.6 relationship-quirk), oneToOne uniqueness not duplicated
  * maybe_auto_link_club_member: exact match links (verified), existing link
    is never clobbered
  * discord_data.level materialized on every XP write (increment + flush) and
    the derived value on get_member
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from uuid import uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402 - loads .env (endpoint/key) before the store
from data.levels import level_from_xp  # noqa: E402
from data.store import StoreError, store  # noqa: E402


def _uid(tag: str) -> str:
    return f"tst-{tag}-{uuid4().hex[:14]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemberLinkLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(store.init())
        cls.created_members: list[str] = []
        cls.created_discord_users: list[str] = []
        cls.created_discord_data: list[str] = []
        cls.created_links: list[str] = []

        cls.link_uid = _uid("du")
        cls.member_a = _uid("ma")
        cls.member_b = _uid("mb")
        cls.auto_member = _uid("am")
        cls.level_uid = _uid("lv")

        # Club registry rows (members.name / created_at / updated_at are REQ).
        # auto_member's name is deliberately close-but-distinct so member_a is
        # the unique best match for "Zzqx Auto Testperson".
        for mid, name in ((cls.member_a, "Zzqx Auto Testperson"),
                          (cls.member_b, "Qzzt Second Testperson"),
                          (cls.auto_member, "Zzqx Auto Testperson B")):
            asyncio.run(store._create("members", mid, {
                "name": name, "created_at": _now(), "updated_at": _now()}))
            cls.created_members.append(mid)

        asyncio.run(store._create("discord_users", cls.link_uid,
                                  {"username": "zzqxtestlink"}))
        cls.created_discord_users.append(cls.link_uid)
        asyncio.run(store._create("discord_users", cls.level_uid,
                                  {"username": "zzqxtestxp"}))
        cls.created_discord_users.append(cls.level_uid)

    @classmethod
    def tearDownClass(cls):
        async def cleanup():
            # Link rows first (their FKs reference the members/users below).
            for link_id in cls.created_links:
                try:
                    await store._raw()[0].delete_row(
                        store._raw()[1], "member_discord_links", link_id)
                except Exception:
                    pass
            for mid in cls.created_members:
                try:
                    await store._raw()[0].delete_row(
                        store._raw()[1], "members", mid)
                except Exception:
                    pass
            for uid in cls.created_discord_users:
                try:
                    await store._raw()[0].delete_row(
                        store._raw()[1], "discord_users", uid)
                except Exception:
                    pass
            for uid in cls.created_discord_data:
                try:
                    await store._raw()[0].delete_row(
                        store._raw()[1], "discord_data", uid)
                except Exception:
                    pass
        asyncio.run(cleanup())

    # ── link write / re-point / unlink ────────────────────────
    def test_01_link_creates_row_with_both_fks(self):
        link_id = asyncio.run(store.link_member_discord(
            self.member_a, self.link_uid, verified=True))
        self.created_links.append(link_id)
        row = asyncio.run(store.discord_member_link(self.link_uid))
        self.assertIsNotNone(row)
        self.assertEqual(row["$id"], link_id)
        self.assertTrue(row.get("is_verified"))
        # FKs are plain id strings in the listed shape.
        self.assertEqual(row.get("discord_user"), self.link_uid)
        self.assertEqual(row.get("member"), self.member_a)

    def test_02_repatch_keeps_fks(self):
        # TablesDB 1.9.6 NULLs relationship columns on PATCH if not re-sent;
        # the store's link writer must preserve them.
        asyncio.run(store.link_member_discord(
            self.member_a, self.link_uid, verified=False))
        row = asyncio.run(store.discord_member_link(self.link_uid))
        self.assertFalse(row.get("is_verified"))
        self.assertEqual(row.get("member"), self.member_a)
        self.assertEqual(row.get("discord_user"), self.link_uid)

    def test_03_onetoone_repoints_not_duplicates(self):
        asyncio.run(store.link_member_discord(
            self.member_b, self.link_uid, verified=True))
        rows = asyncio.run(store._listed("member_discord_links", 25))
        # rows list all hub links; ours must be exactly one for this uid.
        mine = [r for r in rows if r.get("discord_user") == self.link_uid]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].get("member"), self.member_b)
        self.assertTrue(mine[0].get("is_verified"))
        self.assertEqual(
            asyncio.run(store.member_discord_link(self.member_b))["$id"],
            mine[0]["$id"])

    def test_04_unlink_removes(self):
        removed = asyncio.run(store.unlink_member_discord(self.link_uid))
        self.assertEqual(removed, 1)
        self.assertIsNone(asyncio.run(store.discord_member_link(self.link_uid)))
        self.assertIsNone(asyncio.run(store.member_discord_link(self.member_b)))

    # ── auto-link by name ──────────────────────────────────────
    def test_05_auto_link_exact_match_verified(self):
        link_id = asyncio.run(store.maybe_auto_link_club_member(
            self.link_uid, "Zzqx Auto Testperson"))
        self.assertIsNotNone(link_id)
        self.created_links.append(link_id)
        row = asyncio.run(store.discord_member_link(self.link_uid))
        self.assertTrue(row.get("is_verified"))  # exact → verified
        self.assertEqual(row.get("member"), self.member_a)

    def test_06_auto_link_never_clobbers(self):
        # member_a and auto_member share the exact same name — the unlinked
        # _auto_member is a plausible target, but an existing link wins.
        self.assertIsNone(asyncio.run(store.maybe_auto_link_club_member(
            self.link_uid, "Zzqx Auto Testperson")))
        row = asyncio.run(store.discord_member_link(self.link_uid))
        self.assertEqual(row.get("member"), self.member_a)

    def test_07_auto_link_unknown_name_ignored(self):
        self.assertIsNone(asyncio.run(store.maybe_auto_link_club_member(
            self.link_uid, "Nobody Fits Here")))

    def test_08_get_member_carries_club_identity(self):
        rec = asyncio.run(store.get_member(self.link_uid))
        self.assertEqual(rec.get("club_member_id"), self.member_a)
        self.assertEqual(rec.get("club_member_name"), "Zzqx Auto Testperson")

    # ── level persistence ──────────────────────────────────────
    def test_09_increment_persists_level(self):
        asyncio.run(store.increment_member(self.level_uid, "xp", 150))
        self.created_discord_data.append(self.level_uid)
        row = asyncio.run(store._get("discord_data", self.level_uid))
        self.assertEqual(row.get("xp"), 150)
        self.assertEqual(row.get("level"), level_from_xp(150))

    def test_10_messages_do_not_touch_level(self):
        asyncio.run(store.increment_member(self.level_uid, "messages", 7))
        row = asyncio.run(store._get("discord_data", self.level_uid))
        self.assertEqual(row.get("messages"), 7)
        self.assertEqual(row.get("level"), level_from_xp(150))

    def test_11_flush_persists_level(self):
        asyncio.run(store.flush_member_activity(
            {self.level_uid: {"xp": 850}}))
        row = asyncio.run(store._get("discord_data", self.level_uid))
        self.assertEqual(row.get("xp"), 1000)
        self.assertEqual(row.get("level"), level_from_xp(1000))

    def test_12_get_member_reports_derived_level(self):
        rec = asyncio.run(store.get_member(self.level_uid))
        self.assertEqual(rec.get("xp"), 1000)
        self.assertEqual(rec.get("level"), level_from_xp(1000))

    def test_13_link_with_missing_member_is_rejected(self):
        ghost = _uid("ghost")
        with self.assertRaises(StoreError) as cm:
            asyncio.run(store.link_member_discord(
                ghost, self.level_uid, verified=True))
        self.assertIn("not found", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)