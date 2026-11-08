import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("APPWRITE_API_KEY", "y")

from scripts.onboarding_followup import (  # noqa: E402
    WELCOME_TITLE,
    missing_name_members,
    repair_welcomes,
)


class FollowupTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_names_exclude_bots_and_named_members(self):
        members = [SimpleNamespace(id=1, bot=False), SimpleNamespace(id=2, bot=False),
                   SimpleNamespace(id=3, bot=True), SimpleNamespace(id=4, bot=False)]
        rows = [SimpleNamespace(id="1", data={"real_name": "Ada"}),
                SimpleNamespace(id="2", data={"real_name": "  "})]
        tables = SimpleNamespace(list_rows=Mock(return_value=SimpleNamespace(rows=rows)))
        with patch("scripts.onboarding_followup.build_tables_client", return_value=tables):
            result = await missing_name_members(members)
        self.assertEqual([member.id for member in result], [2, 4])

    async def test_database_error_stops_selection(self):
        tables = SimpleNamespace(list_rows=Mock(side_effect=RuntimeError("database unavailable")))
        with patch("scripts.onboarding_followup.build_tables_client", return_value=tables):
            with self.assertRaises(RuntimeError):
                await missing_name_members([SimpleNamespace(id=1, bot=False)])

    async def test_repair_only_edits_departed_members_without_pinging(self):
        departed = SimpleNamespace(
            author=SimpleNamespace(id=5), embeds=[discord.Embed(
                title=WELCOME_TITLE, description="Hey <@123>, welcome!")], edit=AsyncMock())
        present = SimpleNamespace(
            author=SimpleNamespace(id=5), embeds=[discord.Embed(
                title=WELCOME_TITLE, description="Hey <@456>, welcome!")], edit=AsyncMock())

        async def history(**_kwargs):
            yield departed
            yield present

        channel = SimpleNamespace(name="welcome", history=history)
        guild = SimpleNamespace(fetch_channels=AsyncMock(return_value=[channel]))
        client = SimpleNamespace(user=SimpleNamespace(id=5), fetch_user=AsyncMock(
            return_value=SimpleNamespace(global_name="Former Member", name="former")))
        with patch("scripts.onboarding_followup.config.CHANNEL_WELCOME", "welcome"):
            result = await repair_welcomes(client, guild, {456}, apply=True)
        self.assertEqual(result, (1, 1, 0))
        self.assertEqual(departed.embeds[0].description, "Hey Former Member, welcome!")
        self.assertFalse(departed.edit.await_args.kwargs["allowed_mentions"].users)
        present.edit.assert_not_awaited()
