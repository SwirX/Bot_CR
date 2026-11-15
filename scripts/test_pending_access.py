import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs import _club_ids as ids
from cogs import _pending_access as pending


class PendingAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_restrict_and_restore_preserves_unrelated_permissions(self):
        stored = {}
        async def get_setting(key):
            return stored.get(key)
        async def set_setting(key, value):
            stored[key] = value
        class Channel:
            id, category_id = 9, None
            def __init__(self):
                self.overwrite = discord.PermissionOverwrite(send_messages=True)
            def permissions_for(self, _member):
                return SimpleNamespace(view_channel=self.overwrite.view_channel is not False)
            def overwrites_for(self, _member):
                return discord.PermissionOverwrite.from_pair(*self.overwrite.pair())
            async def set_permissions(self, _member, *, overwrite, reason):
                self.overwrite = overwrite or discord.PermissionOverwrite()
        channel = Channel()
        guild = SimpleNamespace(channels=[channel], get_channel=lambda _: channel)
        member = SimpleNamespace(id=111, bot=False, guild=guild,
                                 guild_permissions=SimpleNamespace(administrator=False),
                                 roles=[SimpleNamespace(id=ids.PENDING)])
        guild.get_member = lambda _: member
        fake_store = SimpleNamespace(get_setting=get_setting, set_setting=set_setting)
        with patch.object(pending, "store", fake_store):
            await pending.restrict_pending(member)
            self.assertIs(channel.overwrite.view_channel, False)
            self.assertIs(channel.overwrite.send_messages, True)
            await pending.release_pending(member)
        self.assertIsNone(channel.overwrite.view_channel)
        self.assertIs(channel.overwrite.send_messages, True)

    def test_entry_and_support_channels_are_allowed(self):
        self.assertTrue(pending.allowed_while_pending(SimpleNamespace(id=ids.RULES, category_id=0)))
        self.assertTrue(pending.allowed_while_pending(SimpleNamespace(id=pending.ENTRY_CATEGORY, category_id=None)))
        self.assertTrue(pending.allowed_while_pending(SimpleNamespace(id=88, category_id=pending.SUPPORT_CATEGORY)))
        self.assertFalse(pending.allowed_while_pending(SimpleNamespace(id=88, category_id=None)))
