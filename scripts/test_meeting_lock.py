import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs.meetings import Meetings


class MeetingLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_lock_blocks_default_role_and_explicit_grants(self):
        everyone, role, invited, bot = [object() for _ in range(4)]
        channel = SimpleNamespace(overwrites={
            everyone: discord.PermissionOverwrite(view_channel=True, connect=True),
            role: discord.PermissionOverwrite(connect=True),
            invited: discord.PermissionOverwrite(view_channel=True, connect=True),
            bot: discord.PermissionOverwrite(connect=True),
        }, set_permissions=AsyncMock())
        guild = SimpleNamespace(default_role=everyone, me=bot)
        author = SimpleNamespace(id=42, roles=[], bot=False)
        ctx = SimpleNamespace(guild=guild, author=author, interaction=None, send=AsyncMock())
        cog = Meetings(SimpleNamespace())
        cog._live_meeting = AsyncMock(return_value={"id": "m", "channel_id": "1", "created_by": "42"})
        cog._find_channel = lambda _: channel
        with patch("cogs.meetings.store.set_meeting_locked", new=AsyncMock()):
            await Meetings.meeting_lock.callback(cog, ctx)
        writes = channel.set_permissions.call_args_list
        self.assertEqual({call.args[0] for call in writes}, {everyone, role, invited})
        self.assertTrue(all(call.kwargs["overwrite"].connect is False for call in writes))
        self.assertTrue(channel.overwrites[invited].view_channel)
        self.assertIn("Meeting locked", ctx.send.await_args.args[0])

    async def test_other_chief_cannot_lock(self):
        cog = Meetings(SimpleNamespace())
        cog._live_meeting = AsyncMock(return_value={"created_by": "1"})
        ctx = SimpleNamespace(author=SimpleNamespace(id=2, roles=[], bot=False), send=AsyncMock())
        await Meetings.meeting_lock.callback(cog, ctx)
        self.assertIn("Only the organizer", ctx.send.await_args.args[0])
