import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs import _verification_panel as panel
from cogs.details_reminder import MESSAGE
from cogs.onboarding import Onboarding


class VerificationPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_message_is_updated_without_duplicate(self):
        message = SimpleNamespace(author=SimpleNamespace(id=1), edit=AsyncMock())
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message), send=AsyncMock())
        guild = SimpleNamespace(get_channel=lambda _: channel)
        bot = SimpleNamespace(get_guild=lambda _: guild, user=SimpleNamespace(id=1))
        store = SimpleNamespace(get_setting=AsyncMock(return_value="23"), set_setting=AsyncMock())
        with patch.object(panel, "store", store):
            await panel.ensure_panel(bot, lambda: "view")
        message.edit.assert_awaited_once_with(content=panel.TEXT, view="view")
        channel.send.assert_not_awaited()

    async def test_join_dm_is_the_details_reminder(self):
        role = SimpleNamespace(id=2, name="pending")
        guild = SimpleNamespace(id=3, get_role=lambda _: role, roles=[role])
        member = SimpleNamespace(id=4, guild=guild, bot=False, name="new", add_roles=AsyncMock(), send=AsyncMock())
        cog = Onboarding(SimpleNamespace(add_view=lambda _: None))
        with patch("cogs.onboarding.store", SimpleNamespace(merge_member=AsyncMock())):
            await cog.on_member_join(member)
        self.assertEqual(member.send.await_args.args[0], MESSAGE)
        self.assertEqual(member.send.await_args.kwargs["view"].start.custom_id, "onboarding_start")
