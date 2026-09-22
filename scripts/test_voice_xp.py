"""Hermetic checks for voice-channel XP accrual and the daily cap.

Run:  .venv-local/bin/python scripts/test_voice_xp.py
No network: a bare Engagement instance (bypassing __init__'s task loop) is
built with a stub bot, and the store read is mocked, so nothing touches
Discord, Appwrite or the real database.
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from cogs.engagement import Engagement, cap_grant  # noqa: E402


class _StubBot:
    def __init__(self):
        self.guilds = []

    def get_guild(self, guild_id):
        return None


class _Member:
    id = 4
    name = "tester"
    mention = "<@4>"
    guild = None


def _bare_cog():
    """Engagement without __init__ so the task loop never starts."""
    cog = Engagement.__new__(Engagement)
    cog.bot = _StubBot()
    cog.voice_sessions = {}
    cog.voice_day_granted = {}
    cog.xp_pending = {}
    cog.xp_base = {}
    cog.level_seen = {}
    cog.name_cache = {}
    cog._voice_per_second = config.VOICE_XP_PER_MINUTE / 60.0
    return cog


class VoiceAccrualTests(unittest.TestCase):
    def setUp(self):
        self.old_per_minute = config.VOICE_XP_PER_MINUTE
        config.VOICE_XP_PER_MINUTE = 2  # 2 XP / 60 s -> 1/30 XP per second
        self.cog = _bare_cog()

    def tearDown(self):
        config.VOICE_XP_PER_MINUTE = self.old_per_minute

    def test_two_minutes_earns_four_xp(self):
        self.cog.voice_sessions[1] = [0.0, 0.0, 0]
        self.assertEqual(self.cog._accrue_voice(1, 60.0), 2)
        self.assertEqual(self.cog._accrue_voice(1, 120.0), 2)

    def test_fractional_remainder_carries_into_next_tick(self):
        self.cog.voice_sessions[2] = [0.0, 0.0, 0]
        self.assertEqual(self.cog._accrue_voice(2, 45.0), 1)   # 1.5 -> 1, keeps .5
        self.assertEqual(self.cog._accrue_voice(2, 60.0), 1)   # .5 + .5 -> 1

    def test_tick_advances_session_start(self):
        self.cog.voice_sessions[3] = [0.0, 0.0, 0]
        self.cog._accrue_voice(3, 30.0)
        self.assertEqual(self.cog.voice_sessions[3][0], 30.0)

    def test_missing_session_is_noop(self):
        self.assertEqual(self.cog._accrue_voice(99, 60.0), 0)

    def test_close_credits_leftover_whole_xp(self):
        from data.store import store

        self.cog.voice_sessions[4] = [0.0, 0.5, 0]  # 0.5 XP carried from a tick
        with mock.patch.object(store, "get_member", return_value=None):
            asyncio.run(self.cog._close_voice_session(_Member(), None, 45.0))
        # 0.5 + 45 s * (1/30 per s) = 2.0 -> 2 XP granted, session dropped.
        self.assertEqual(self.cog.xp_pending.get(4), 2)
        self.assertNotIn(4, self.cog.voice_sessions)

    def test_close_without_session_is_noop(self):
        asyncio.run(self.cog._close_voice_session(_Member(), None, 60.0))
        self.assertEqual(self.cog.xp_pending, {})


class CapTests(unittest.TestCase):
    def test_cap_grant_respects_daily_limit(self):
        self.assertEqual(cap_grant(0, 120, 50), 50)
        self.assertEqual(cap_grant(110, 120, 50), 10)
        self.assertEqual(cap_grant(120, 120, 50), 0)
        self.assertEqual(cap_grant(0, 120, 0), 0)

    def test_rate_is_per_minute_over_sixty(self):
        self.assertAlmostEqual(config.VOICE_XP_PER_MINUTE / 60.0, 1.0 / 30.0)


if __name__ == "__main__":
    unittest.main()