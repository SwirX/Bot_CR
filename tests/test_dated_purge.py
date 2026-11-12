from datetime import datetime
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.moderation import Moderation
from cogs._purge import all_targets, day_bounds, outside_bureau, purge, ZONE


class DatedPurgeTests(IsolatedAsyncioTestCase):
    async def test_date_only_purges_whole_day_in_voice_chat(self):
        channel = Mock(spec=discord.VoiceChannel)
        channel.name = "voice"
        channel.id = 10
        channel.permissions_for.return_value = discord.Permissions.all()
        channel.purge = AsyncMock(return_value=[])
        ctx = SimpleNamespace(channel=channel, interaction=object(),
                              guild=SimpleNamespace(me=object()), author=object(),
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

    def test_relative_dates_and_discord_timestamp(self):
        now = datetime(2026, 11, 12, 15, tzinfo=ZONE)
        for value, day in (("today", "2026-11-12"), ("yesterday", "2026-11-11"),
                           ("2 days ago", "2026-11-10"), ("today last week", "2026-11-05"),
                           (f"<t:{int(now.timestamp())}:F>", "2026-11-12")):
            self.assertEqual(day_bounds(value, now)[0].strftime("%F"), day)

    async def test_count_is_matching_messages_across_channels(self):
        author = SimpleNamespace(id=7)
        bot = SimpleNamespace(id=8)
        wanted = SimpleNamespace(id=9)
        channels = []
        messages = []
        for channel_id, ids in ((1, (20, 14, 11)), (2, (19, 18, 12))):
            channel = Mock(spec=discord.VoiceChannel)
            channel.id = channel_id
            channel.permissions_for.return_value = discord.Permissions.all()
            rows = [SimpleNamespace(id=i, author=SimpleNamespace(id=9 if i != 20 else 10),
                                    delete=AsyncMock()) for i in ids]
            messages.extend(rows)
            async def history(*args, rows=rows, **kwargs):
                for row in rows:
                    yield row
            channel.history = history
            channels.append(channel)
        with patch("cogs._purge.is_bot_admin", return_value=False):
            result = await purge(channels, author, bot, 2, None, None, wanted, None)
        self.assertEqual(result, (2, 0, 0))
        self.assertEqual([m.id for m in messages if m.delete.await_count], [19, 18])

    def test_bureau_is_excluded(self):
        channel = Mock(spec=discord.TextChannel)
        channel.category_id = 1336709394926141532
        self.assertFalse(outside_bureau(channel))

    async def test_all_targets_excludes_bureau_and_deduplicates(self):
        ordinary = Mock(spec=discord.VoiceChannel)
        ordinary.id = 1
        ordinary.category_id = 5
        bureau = Mock(spec=discord.TextChannel)
        bureau.id = 2
        bureau.category_id = 1336709394926141532
        guild = SimpleNamespace(channels=[ordinary, bureau, ordinary], threads=[])
        targets, failures = await all_targets(guild, object())
        self.assertEqual(targets, [ordinary])
        self.assertEqual(failures, 0)

    async def test_missing_channel_permission_never_deletes(self):
        channel = Mock(spec=discord.TextChannel)
        channel.permissions_for.return_value = discord.Permissions.none()
        channel.purge = AsyncMock()
        self.assertEqual(await purge([channel], object(), object(), None, None, None, None, None),
                         (0, 1, 0))
        channel.purge.assert_not_awaited()
