import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from cogs import _club_ids as ids
from cogs._ticket_views import STAFF_ROLES, TOPIC_PREFIX, may_close, ticket_owner
from cogs.tickets import CATEGORY_ID, Tickets


class Target:
    def __init__(self, identifier):
        self.id = identifier
        self.bot = False
        self.roles = []
        self.guild_permissions = SimpleNamespace(administrator=False)
        self.mention = f"<@{identifier}>"


class TicketTests(unittest.IsolatedAsyncioTestCase):
    def test_close_is_limited_to_creator_and_staff(self):
        channel = SimpleNamespace(topic=f"{TOPIC_PREFIX}123", category_id=CATEGORY_ID)
        owner, stranger, staff = Target(123), Target(456), Target(789)
        staff.roles = [Target(ids.IT_MEMBER)]
        self.assertEqual(ticket_owner(channel), 123)
        self.assertTrue(may_close(owner, channel))
        self.assertTrue(may_close(staff, channel))
        self.assertFalse(may_close(stranger, channel))
        self.assertFalse(may_close(staff, SimpleNamespace(topic="ordinary channel", category_id=CATEGORY_ID)))

    async def test_ticket_is_private_and_duplicate_opens_are_reused(self):
        category = MagicMock(spec=discord.CategoryChannel)
        category.text_channels = []
        owner, bot, everyone = Target(123), Target(999), Target(ids.GUILD)
        roles = {uid: Target(uid) for uid in STAFF_ROLES}
        channel = SimpleNamespace(id=55, mention="<#55>", send=AsyncMock(), delete=AsyncMock())
        guild = SimpleNamespace(id=ids.GUILD, default_role=everyone, me=bot,
                                get_member=lambda uid: owner if uid == owner.id else None,
                                get_channel=lambda uid: category if uid == CATEGORY_ID else None,
                                get_role=roles.get, create_text_channel=AsyncMock(return_value=channel))
        interaction = SimpleNamespace(guild=guild, user=owner,
                                      followup=SimpleNamespace(send=AsyncMock()))
        cog = Tickets(SimpleNamespace(add_view=lambda _: None))
        await asyncio.gather(cog.open_ticket(interaction, "Help", "Details"),
                             cog.open_ticket(interaction, "Help", "Details"))
        guild.create_text_channel.assert_awaited_once()
        params = guild.create_text_channel.await_args.kwargs
        permissions = params["overwrites"]
        self.assertFalse(permissions[everyone].view_channel)
        self.assertTrue(permissions[owner].view_channel)
        self.assertTrue(permissions[roles[ids.IT_MEMBER]].view_channel)
        self.assertEqual(params["topic"], f"{TOPIC_PREFIX}123")
        channel.send.assert_awaited_once()
        self.assertEqual(interaction.followup.send.await_count, 2)
        self.assertTrue(any("déjà" in call.args[0] for call in interaction.followup.send.await_args_list))

    async def test_missing_staff_role_refuses_creation(self):
        category = MagicMock(spec=discord.CategoryChannel)
        category.text_channels = []
        owner = Target(123)
        guild = SimpleNamespace(id=ids.GUILD, get_member=lambda _: owner,
                                get_channel=lambda _: category, get_role=lambda _: None,
                                create_text_channel=AsyncMock())
        interaction = SimpleNamespace(guild=guild, user=owner, followup=SimpleNamespace(send=AsyncMock()))
        cog = Tickets(SimpleNamespace(add_view=lambda _: None))
        await cog.open_ticket(interaction, "Help", "Details")
        guild.create_text_channel.assert_not_awaited()
        self.assertIn("indisponible", interaction.followup.send.await_args.args[0])

    async def test_existing_panel_is_reused_on_restart(self):
        from unittest.mock import patch
        category = MagicMock(spec=discord.CategoryChannel)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id, channel.category_id = 55, CATEGORY_ID
        channel.fetch_message = AsyncMock()
        channel.send = AsyncMock()
        message = SimpleNamespace(id=66, author=SimpleNamespace(id=999), edit=AsyncMock())
        channel.fetch_message.return_value = message
        guild = SimpleNamespace(get_channel=lambda uid: {CATEGORY_ID: category, 55: channel}.get(uid))
        bot = SimpleNamespace(add_view=lambda _: None, get_guild=lambda _: guild,
                              user=SimpleNamespace(id=999))
        settings = SimpleNamespace(get_setting=AsyncMock(side_effect=["55", "66"]),
                                   set_setting=AsyncMock())
        with patch("cogs.tickets.store", settings):
            await Tickets(bot).ensure_panel()
        message.edit.assert_awaited_once()
        channel.send.assert_not_awaited()

    async def test_empty_form_does_not_create_ticket(self):
        guild = SimpleNamespace(id=ids.GUILD, create_text_channel=AsyncMock())
        interaction = SimpleNamespace(guild=guild, followup=SimpleNamespace(send=AsyncMock()))
        await Tickets(SimpleNamespace(add_view=lambda _: None)).open_ticket(interaction, " ", "Help")
        guild.create_text_channel.assert_not_awaited()
        self.assertIn("renseigner", interaction.followup.send.await_args.args[0])
