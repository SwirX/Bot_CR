import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("APPWRITE_API_KEY", "y")

from cogs._message_moderation import delete_message  # noqa: E402


def make_context(*, permitted=True):
    permissions = SimpleNamespace(view_channel=permitted, read_message_history=permitted,
                                  manage_messages=permitted)
    message = SimpleNamespace(delete=AsyncMock())
    channel = SimpleNamespace(id=123, permissions_for=Mock(return_value=permissions),
                              fetch_message=AsyncMock(return_value=message))
    ctx = SimpleNamespace(author=SimpleNamespace(id=5), channel=channel,
                          defer=AsyncMock(), send=AsyncMock())
    return ctx, channel, message


class DeleteMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_only_requested_message_in_selected_channel(self):
        ctx, _, _ = make_context()
        _, target, message = make_context()
        with patch("cogs._message_moderation.is_bot_admin", return_value=False):
            await delete_message(ctx, "1333538137091215380", target)
        target.fetch_message.assert_awaited_once_with(1333538137091215380)
        message.delete.assert_awaited_once()
        self.assertIn("Message deleted", ctx.send.await_args.args[0])

    async def test_target_channel_permission_required(self):
        ctx, _, _ = make_context()
        _, target, _ = make_context(permitted=False)
        with patch("cogs._message_moderation.is_bot_admin", return_value=False):
            await delete_message(ctx, "123", target)
        target.fetch_message.assert_not_awaited()
        self.assertIn("permission", ctx.send.await_args.args[0])

    async def test_invalid_id_never_fetches_message(self):
        ctx, target, _ = make_context()
        with patch("cogs._message_moderation.is_bot_admin", return_value=False):
            for value in ("abc", "0", "9" * 21, "１２３"):
                await delete_message(ctx, value)
        target.fetch_message.assert_not_awaited()
        self.assertIn("valid numeric", ctx.send.await_args.args[0])

    async def test_missing_message_is_reported(self):
        ctx, target, _ = make_context()
        response = SimpleNamespace(status=404, reason="Not Found")
        target.fetch_message.side_effect = discord.NotFound(response, "Unknown Message")
        with patch("cogs._message_moderation.is_bot_admin", return_value=False):
            await delete_message(ctx, "123")
        self.assertIn("couldn't find", ctx.send.await_args.args[0])

    async def test_bot_staff_can_use_their_existing_bypass(self):
        ctx, target, message = make_context(permitted=False)
        with patch("cogs._message_moderation.is_bot_admin", return_value=True):
            await delete_message(ctx, "123")
        target.fetch_message.assert_awaited_once_with(123)
        message.delete.assert_awaited_once()

    async def test_discord_permission_failure_is_reported(self):
        ctx, target, _ = make_context()
        response = SimpleNamespace(status=403, reason="Forbidden")
        target.fetch_message.side_effect = discord.Forbidden(response, "Missing Access")
        with patch("cogs._message_moderation.is_bot_admin", return_value=True):
            await delete_message(ctx, "123")
        self.assertIn("permissions", ctx.send.await_args.args[0])
