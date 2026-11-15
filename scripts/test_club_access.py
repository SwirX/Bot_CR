import unittest
from types import SimpleNamespace

import discord

from cogs import _club_ids as ids
from scripts.apply_club_access import build_plan, CELL_MEMBERS, CELL_HEADS, BUREAU


class Target:
    def __init__(self, identifier):
        self.id = identifier


class Channel:
    def __init__(self, identifier, category_id=None):
        self.id, self.category_id = identifier, category_id
    def overwrites_for(self, _target):
        return discord.PermissionOverwrite()


class AccessPlanTests(unittest.TestCase):
    def test_scope_for_bureau_staff_development_and_old_voice(self):
        role_ids = {ids.PENDING, ids.VERIFIED, ids.OLD_MEMBER, ids.NEW_MEMBER,
                    ids.BOT_DEVELOPER, *CELL_MEMBERS, *CELL_HEADS, *BUREAU}
        roles = [Target(role_id) for role_id in role_ids]
        default = Target(1)
        bureau = Channel(2, ids.BUREAU_CATEGORY)
        staff = Channel(3, ids.STAFF_CATEGORY)
        development = Channel(ids.BOT_DEVELOPMENT)
        old_voice = Channel(ids.OLD_MEMBERS_VOICE)
        rules = Channel(ids.RULES)
        guild = SimpleNamespace(id=1, roles=roles, default_role=default,
                                channels=[bureau, staff, development, old_voice, rules])
        plan = {(channel.id, role.id): desired for channel, role, _, desired in build_plan(guild)}
        self.assertFalse(plan[(bureau.id, default.id)].view_channel)
        self.assertFalse(plan[(bureau.id, ids.PENDING)].view_channel)
        self.assertTrue(plan[(bureau.id, ids.PRESIDENT)].view_channel)
        self.assertNotIn((bureau.id, ids.IT_MEMBER), plan)
        self.assertTrue(plan[(staff.id, ids.IT_HEAD)].view_channel)
        self.assertTrue(plan[(development.id, ids.IT_MEMBER)].send_messages)
        self.assertTrue(plan[(old_voice.id, ids.VERIFIED)].view_channel)
        self.assertFalse(plan[(old_voice.id, ids.VERIFIED)].connect)
        self.assertFalse(plan[(old_voice.id, ids.VERIFIED)].read_message_history)
        self.assertFalse(plan[(old_voice.id, ids.VERIFIED)].send_messages)
        self.assertTrue(plan[(old_voice.id, ids.OLD_MEMBER)].connect)
        self.assertTrue(plan[(old_voice.id, ids.OLD_MEMBER)].read_message_history)
        self.assertTrue(plan[(rules.id, ids.PENDING)].view_channel)
        self.assertTrue(plan[(rules.id, ids.PENDING)].read_message_history)
