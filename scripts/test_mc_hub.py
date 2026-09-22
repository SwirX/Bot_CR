"""Live contract checks for the hub-side Minecraft link display fallback.

Run:  .venv-local/bin/python scripts/test_mc_hub.py
Touches robotics_hub with disposable ``tst-…`` ids only; every row created is
deleted at the end. Covers:

  * mc_active_link_for_user: reads the ACTIVE discord_mc_links row, shapes it
    like the legacy links.minecraft slot (username/type/uuids/linked_at)
  * inactive (unlinked) rows are invisible to it — unlink can't resurrect
  * unknown Discord users return None

This is the store half of the fix; the cog half (mc_link_card_embed) is a
thin wrapper that prefers the sidecar slot and falls back to this.
"""

import asyncio
import os
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402 - loads .env before the store
from data.store import store  # noqa: E402

USERNAME = "SwirXHubTest"


def _uid(tag: str) -> str:
    return f"tst-{tag}-{uuid4().hex[:14]}"


class McHubDisplayLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        asyncio.run(store.init())
        cls.discord_uid = _uid("mchub")
        cls.pair_id = asyncio.run(store.mc_create_pair(
            cls.discord_uid, USERNAME, "TESTCODE1",
            username_hint="zzqxtest"))  # also creates the discord_users row
        # Simulate the plugin claiming the pairing in-game (contract §5.1.3).
        asyncio.run(store._patch("discord_mc_links", cls.pair_id,
                                 {"is_active": True,
                                  "verified_at":
                                      "2026-09-21T23:43:28.891+00:00"}))

    @classmethod
    def tearDownClass(cls):
        async def cleanup():
            tdb, db_id = store._raw()
            for row_id in (cls.pair_id,):
                try:
                    await tdb.delete_row(db_id, "discord_mc_links", row_id)
                except Exception:
                    pass
            try:
                await tdb.delete_row(db_id, "minecraft_accounts", USERNAME)
            except Exception:
                pass
            try:
                await tdb.delete_row(db_id, "discord_users", cls.discord_uid)
            except Exception:
                pass
        asyncio.run(cleanup())

    def test_01_active_link_shaped_like_legacy_slot(self):
        link = asyncio.run(store.mc_active_link_for_user(self.discord_uid))
        self.assertIsNotNone(link)
        self.assertEqual(link["username"], USERNAME)
        self.assertEqual(link["type"], "free")  # cracked account
        self.assertIn("linked_at", link)
        self.assertIsInstance(link.get("uuids"), list)

    def test_02_inactive_link_is_invisible(self):
        asyncio.run(store.mc_deactivate_link(self.discord_uid, USERNAME))
        self.assertIsNone(
            asyncio.run(store.mc_active_link_for_user(self.discord_uid)))
        # And re-activating makes it visible again (unlink then re-link).
        asyncio.run(store._patch("discord_mc_links", self.pair_id,
                                 {"is_active": True}))
        self.assertIsNotNone(
            asyncio.run(store.mc_active_link_for_user(self.discord_uid)))

    def test_03_unknown_user_returns_none(self):
        self.assertIsNone(
            asyncio.run(store.mc_active_link_for_user(_uid("nobody"))))


if __name__ == "__main__":
    unittest.main(verbosity=2)