import json
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.message_expiry import MEMBERS, MessageExpiry


class MessageExpiryTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Mock()
        self.cog = MessageExpiry.__new__(MessageExpiry)
        self.cog.bot = self.bot
        self.cog.pending = {}
        self.cog.loaded = False
        self.cog.dirty = False
        self.cog.lock = asyncio.Lock()

    async def test_schedules_only_target_members_and_persists_due_time(self):
        now = discord.utils.utcnow()
        message = SimpleNamespace(
            guild=SimpleNamespace(id=123), author=SimpleNamespace(id=next(iter(MEMBERS)), bot=False),
            channel=SimpleNamespace(id=456), id=789, created_at=now, is_system=lambda: False,
        )
        with patch("cogs.message_expiry.config.GUILD_ID", 123), \
             patch("cogs.message_expiry.store.get_setting", AsyncMock(return_value=None)), \
             patch("cogs.message_expiry.store.set_setting", AsyncMock()) as save:
            await self.cog.on_message(message)
            entry = json.loads(save.await_args.args[1])["789"]
            self.assertEqual(entry["channel"], 456)
            self.assertAlmostEqual(entry["due"], (now + timedelta(hours=24)).timestamp())
            message.author.id = 1
            await self.cog.on_message(message)
            self.assertEqual(save.await_count, 1)

    async def test_poll_deletes_only_due_and_preserves_future(self):
        delete = AsyncMock()
        channel = Mock()
        channel.get_partial_message.return_value.delete = delete
        self.bot.get_channel.return_value = channel
        self.cog.loaded = True
        now = discord.utils.utcnow().timestamp()
        self.cog.pending = {"1": {"channel": 2, "due": now - 1},
                            "3": {"channel": 2, "due": now + 100}}
        with patch("cogs.message_expiry.store.set_setting", AsyncMock()) as save:
            await self.cog.poll.coro(self.cog)
        channel.get_partial_message.assert_called_once_with(1)
        delete.assert_awaited_once()
        self.assertEqual(list(self.cog.pending), ["3"])
        self.assertEqual(list(json.loads(save.await_args.args[1])), ["3"])
