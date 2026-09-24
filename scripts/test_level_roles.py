"""Hermetic checks for level-up role rewards.

Run:  .venv-local/bin/python scripts/test_level_roles.py
No network: the reward mapper is exercised against fake members/roles, and the
config mapping is monkeypatched per test so nothing touches Discord or
Appwrite.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from cogs.engagement import Engagement  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _Role:
    def __init__(self, role_id: int, name: str):
        self.id = role_id
        self.name = name


class _Member:
    def __init__(self, member_id: int, roles, *, bot=False):
        self.id = member_id
        self.name = f"m{member_id}"
        self.bot = bot
        self.guild = None
        self.roles = list(roles)

    def has_roles(self, *names):
        return all(any(r.name == n for r in self.roles) for n in names)

    async def add_roles(self, role, reason=None):
        if role not in self.roles:
            self.roles.append(role)


class _Guild:
    def __init__(self, roles):
        self.roles = list(roles)

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)


class LevelRoleTests(unittest.TestCase):
    def setUp(self):
        self.old_rewards = config.LEVEL_ROLE_REWARDS
        self.cog = Engagement.__new__(Engagement)
        self.role_5 = _Role(101, "Level 5")
        self.role_10 = _Role(102, "Level 10")
        self.role_20 = _Role(103, "Level 20")
        self.guild = _Guild([self.role_5, self.role_10, self.role_20])

    def tearDown(self):
        config.LEVEL_ROLE_REWARDS = self.old_rewards

    def reward_member(self, member, level):
        _run(self.cog._grant_level_roles(member, level))

    def test_grants_every_reward_below_level(self):
        config.LEVEL_ROLE_REWARDS = {5: 101, 10: 102, 20: 103}
        member = _Member(1, [])
        member.guild = self.guild
        self.reward_member(member, 12)
        self.assertTrue(member.has_roles("Level 5", "Level 10"))
        self.assertFalse(member.has_roles("Level 20"))

    def test_skips_roles_already_held(self):
        config.LEVEL_ROLE_REWARDS = {5: 101, 10: 102}
        member = _Member(1, [self.role_5])
        member.guild = self.guild
        self.reward_member(member, 10)
        self.assertTrue(member.has_roles("Level 5", "Level 10"))
        self.assertEqual(len(member.roles), 2)

    def test_jumping_levels_grants_all_due(self):
        config.LEVEL_ROLE_REWARDS = {5: 101, 10: 102, 20: 103}
        member = _Member(1, [])
        member.guild = self.guild
        self.reward_member(member, 30)
        self.assertTrue(member.has_roles("Level 5", "Level 10", "Level 20"))

    def test_no_rewards_configured_is_noop(self):
        config.LEVEL_ROLE_REWARDS = {}
        member = _Member(1, [])
        member.guild = self.guild
        self.reward_member(member, 10)
        self.assertEqual(member.roles, [])

    def test_guest_member_is_silently_skipped(self):
        config.LEVEL_ROLE_REWARDS = {5: 101}
        guest = _Member(1, [])
        guest.guild = None
        self.reward_member(guest, 10)
        self.assertEqual(guest.roles, [])

    def test_zero_level_grants_nothing(self):
        config.LEVEL_ROLE_REWARDS = {5: 101}
        member = _Member(1, [])
        member.guild = self.guild
        self.reward_member(member, 0)
        self.assertEqual(member.roles, [])

    def test_unknown_role_id_is_noop(self):
        config.LEVEL_ROLE_REWARDS = {5: 999999}
        member = _Member(1, [])
        member.guild = self.guild
        self.reward_member(member, 10)
        self.assertEqual(member.roles, [])


class ConfigParseTests(unittest.TestCase):
    def test_mapping_parses_pairs(self):
        raw = "5:101,10:102,20:103"
        self.assertEqual(config._level_roles(raw), {5: 101, 10: 102, 20: 103})

    def test_mapping_tolerates_garbage(self):
        self.assertEqual(config._level_roles("5:101,,abc,10:102"), {5: 101, 10: 102})
        self.assertEqual(config._level_roles(""), {})
        self.assertEqual(config._level_roles("abc:def"), {})


if __name__ == "__main__":
    unittest.main()