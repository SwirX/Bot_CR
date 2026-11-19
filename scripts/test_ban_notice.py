import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from cogs.moderation import Moderation


class BanNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def prepare(self):
        cog = Moderation(SimpleNamespace())
        cog._can_target = AsyncMock(return_value=True)
        cog._log = AsyncMock()
        moderator = SimpleNamespace(id=10, name="moderator")
        ctx = SimpleNamespace(author=moderator, send=AsyncMock())
        member = SimpleNamespace(id=11, display_name="Member", ban=AsyncMock(), send=AsyncMock())
        await Moderation.ban.callback(cog, ctx, member, reason="Rules")
        view = ctx.send.await_args.kwargs["view"]
        interaction = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))
        return cog, ctx, member, view, interaction

    async def test_success_sends_anonymous_dm_after_ban(self):
        cog, ctx, member, view, interaction = await self.prepare()
        async def send_notice(message):
            member.ban.assert_awaited_once()
            self.assertEqual(message, "You have been banned from the Robotics Club server.")
        member.send.side_effect = send_notice
        await view.on_confirm(interaction)
        cog._log.assert_awaited_once_with(ctx, "ban", member, "Rules")
        self.assertEqual(interaction.followup.send.await_count, 1)

    async def test_closed_dms_are_reported_without_reverting_ban(self):
        cog, ctx, member, view, interaction = await self.prepare()
        member.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "closed")
        await view.on_confirm(interaction)
        member.ban.assert_awaited_once()
        self.assertIn("couldn't send", interaction.followup.send.await_args.args[0])
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        cog._log.assert_awaited_once_with(ctx, "ban", member, "Rules")

    async def test_failed_ban_never_sends_ban_notice(self):
        cog, _, member, view, interaction = await self.prepare()
        member.ban.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
        await view.on_confirm(interaction)
        member.send.assert_not_awaited()
        cog._log.assert_not_awaited()
