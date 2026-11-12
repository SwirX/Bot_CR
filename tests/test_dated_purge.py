from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

import discord

from cogs.moderation import Moderation


class DatedPurgeTests(IsolatedAsyncioTestCase):
    async def test_date_only_purges_whole_day_in_voice_chat(self):
        channel = Mock(spec=discord.VoiceChannel)
        channel.name = "voice"
        channel.purge = AsyncMock(return_value=[])
        ctx = SimpleNamespace(channel=channel, interaction=object(),
                              defer=AsyncMock(), send=AsyncMock())
        await Moderation.delete_messages.callback(Moderation(Mock()), ctx, date="2026-11-11")
        kwargs = channel.purge.await_args.kwargs
        self.assertIsNone(kwargs["limit"])
        self.assertEqual(kwargs["after"].strftime("%F %H:%M"), "2026-11-11 00:00")
        self.assertEqual(kwargs["before"].strftime("%F %H:%M"), "2026-11-12 00:00")
        self.assertEqual(kwargs["after"].tzinfo.key, "Africa/Casablanca")

    async def test_invalid_date_does_not_delete(self):
        channel = Mock(spec=discord.TextChannel)
        channel.purge = AsyncMock()
        ctx = SimpleNamespace(channel=channel, send=AsyncMock())
        await Moderation.delete_messages.callback(Moderation(Mock()), ctx, date="2026-02-30")
        channel.purge.assert_not_awaited()
