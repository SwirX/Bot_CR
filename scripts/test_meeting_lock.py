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
        }, members=[], set_permissions=AsyncMock(), overwrites_for=lambda _: discord.PermissionOverwrite())
        guild = SimpleNamespace(default_role=everyone, me=bot, get_member=lambda _: None)
        author = SimpleNamespace(id=42, roles=[], bot=False)
        ctx = SimpleNamespace(guild=guild, author=author, interaction=None, send=AsyncMock())
        cog = Meetings(SimpleNamespace())
        cog._live_meeting = AsyncMock(return_value={"id": "m", "channel_id": "1", "created_by": "42"})
        cog._find_channel = lambda _: channel
        with patch("cogs.meetings.store.set_meeting_locked", new=AsyncMock()), \
             patch("cogs.meetings.store.list_meeting_sessions", new=AsyncMock(return_value=[])):
            await Meetings.meeting_lock.callback(cog, ctx)
        writes = channel.set_permissions.call_args_list
        self.assertEqual({call.args[0] for call in writes}, {everyone, role, invited})
        self.assertTrue(all(call.kwargs["overwrite"].connect is False for call in writes))
        self.assertTrue(channel.overwrites[invited].view_channel)
        self.assertIn("Meeting locked", ctx.send.await_args.args[0])

    async def test_previous_and_current_attendees_can_reconnect(self):
        class Member:
            def __init__(self, uid):
                self.id, self.bot = uid, False

        everyone, bot = object(), object()
        previous, current = Member(10), Member(11)
        channel = SimpleNamespace(
            overwrites={everyone: discord.PermissionOverwrite(connect=True)},
            members=[current], set_permissions=AsyncMock(),
            overwrites_for=lambda _: discord.PermissionOverwrite(view_channel=True))
        guild = SimpleNamespace(default_role=everyone, me=bot,
                                get_member=lambda uid: {10: previous, 11: current}.get(uid))
        ctx = SimpleNamespace(guild=guild, author=SimpleNamespace(id=42, roles=[], bot=False),
                              interaction=None, send=AsyncMock())
        cog = Meetings(SimpleNamespace())
        cog._live_meeting = AsyncMock(return_value={"id": "m", "channel_id": "1",
                                                       "created_by": "42", "expected": ["10", "11"]})
        cog._find_channel = lambda _: channel
        with patch("cogs.meetings.store.set_meeting_locked", new=AsyncMock()), \
             patch("cogs.meetings.store.list_meeting_sessions", new=AsyncMock(return_value=[{"discord_user": "10"}])):
            await Meetings.meeting_lock.callback(cog, ctx)
        writes = {call.args[0]: call.kwargs["overwrite"].connect
                  for call in channel.set_permissions.call_args_list}
        self.assertEqual(writes, {everyone: False, previous: True, current: True})

    async def test_other_chief_cannot_lock(self):
        cog = Meetings(SimpleNamespace())
        cog._live_meeting = AsyncMock(return_value={"created_by": "1"})
        ctx = SimpleNamespace(author=SimpleNamespace(id=2, roles=[], bot=False), send=AsyncMock())
        await Meetings.meeting_lock.callback(cog, ctx)
        self.assertIn("Only the organizer", ctx.send.await_args.args[0])

class MeetingNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_name_is_passed_to_channel_creation(self):
        everyone, bot, category = object(), object(), object()
        guild = SimpleNamespace(default_role=everyone, me=bot,
                                create_voice_channel=AsyncMock(return_value=object()))
        ctx = SimpleNamespace(guild=guild)
        cog = Meetings(SimpleNamespace())
        with patch("cogs.meetings._meeting_category", return_value=category):
            await cog._make_meeting_channel(ctx, "all", "C Language Lesson")
        self.assertEqual(guild.create_voice_channel.await_args.kwargs["name"], "C Language Lesson")

    async def test_name_and_existing_channel_are_rejected(self):
        ctx = SimpleNamespace(send=AsyncMock())
        cog = Meetings(SimpleNamespace())
        await cog._start_meeting(ctx, channel=object(), name="Lesson")
        self.assertIn("leave `channel` empty", ctx.send.await_args.args[0])
