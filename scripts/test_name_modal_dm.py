"""DM name forms must edit a guild member, not the DM's User object."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("APPWRITE_API_KEY", "y")
os.environ.setdefault("APPWRITE_PROJECT_ID", "robotics-ops")
os.environ.setdefault("APPWRITE_DATABASE_ID", "robotics_hub")

import config  # noqa: E402
from cogs._member_identity import member_for_interaction  # noqa: E402
from cogs.onboarding import NameModal, Onboarding, cursive_nickname  # noqa: E402
from cogs.recordsync import CollectNameModal, CollectNameView  # noqa: E402
from data.store import Store, StoreError  # noqa: E402


def dm_interaction(*, cached=True):
    user = SimpleNamespace(id=42, display_name="Original")
    member = SimpleNamespace(
        id=42, display_name="Original", roles=[], edit=AsyncMock(),
        add_roles=AsyncMock(), remove_roles=AsyncMock(),
        guild=None,
    )
    guild = SimpleNamespace(
        id=123, roles=[SimpleNamespace(name=config.ROLE_VERIFIED)],
        get_member=lambda _id: member if cached else None,
        fetch_member=AsyncMock(return_value=member),
    )
    member.guild = guild
    client = SimpleNamespace(get_guild=lambda _id: guild, guilds=[guild])
    interaction = SimpleNamespace(
        user=user, guild=None, client=client,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    return interaction, member, guild


class DmNameModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_gender_is_written_to_member_record(self):
        store = Store()
        store._write = AsyncMock()
        await store.merge_member(42, {"real_name": "Ada Lovelace", "gender": "female"})
        self.assertEqual(store._write.await_args.args[:3],
                         ("discord_data", "42", {"real_name": "Ada Lovelace", "gender": "female"}))

    async def test_onboarding_edits_member_and_saves_name(self):
        interaction, member, guild = dm_interaction()
        store = SimpleNamespace(merge_member=AsyncMock(),
                                maybe_auto_link_club_member=AsyncMock())
        cog = SimpleNamespace(prompt_birthday=AsyncMock())
        modal = NameModal(cog)
        modal.full_name._value = "Ada Lovelace"
        modal.gender._value = "Female"

        with patch.object(config, "GUILD_ID", guild.id), patch("cogs.onboarding.store", store):
            await modal.on_submit(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        member.edit.assert_awaited_once_with(
            nick=cursive_nickname("Ada Lovelace"),
            reason="Onboarding: real name → cursive nickname",
        )
        self.assertEqual(store.merge_member.await_count, 2)
        self.assertEqual(store.merge_member.await_args_list[0].args,
                         (member.id, {"real_name": "Ada Lovelace", "gender": "female"}))
        self.assertTrue(store.merge_member.await_args_list[1].args[1]["verified"])
        member.add_roles.assert_awaited_once()
        cog.prompt_birthday.assert_not_awaited()
        self.assertIn("Verified", interaction.followup.send.await_args.args[0])

    async def test_birthday_is_optional_and_saved_when_entered(self):
        interaction, member, guild = dm_interaction()
        store = SimpleNamespace(merge_member=AsyncMock(),
                                maybe_auto_link_club_member=AsyncMock())
        modal = NameModal(None)
        modal.full_name._value = "Ada Lovelace"
        modal.gender._value = "female"
        modal.birthday._value = "2000-12-10"
        with patch.object(config, "GUILD_ID", guild.id), patch("cogs.onboarding.store", store):
            await modal.on_submit(interaction)
        self.assertEqual(store.merge_member.await_args_list[0].args[1]["birthday"], "12-10")
        saved = store.merge_member.await_args_list[0].args[1]
        self.assertEqual(saved["birthday_full"], "2000-12-10")
        member.add_roles.assert_awaited_once()

    async def test_invalid_birthday_does_not_save_profile(self):
        interaction, member, _ = dm_interaction()
        modal = NameModal(None)
        modal.full_name._value = "Ada Lovelace"
        modal.gender._value = "female"
        modal.birthday._value = "not-a-date"
        await modal.on_submit(interaction)
        member.edit.assert_not_awaited()
        self.assertIn("valid birthday", interaction.response.send_message.await_args.args[0])

    async def test_join_assigns_new_member_and_pending_roles(self):
        interaction, member, guild = dm_interaction()
        temp = SimpleNamespace(name=config.ROLE_TEMP, id=1)
        new = SimpleNamespace(name="New Member", id=config.ROLE_NEW_MEMBER_ID)
        guild.roles.append(temp)
        guild.get_role = lambda role_id: new if role_id == new.id else None
        member.bot = False
        member.name = "newuser"
        member.send = AsyncMock()
        cog = Onboarding(SimpleNamespace(add_view=lambda _view: None))
        store = SimpleNamespace(merge_member=AsyncMock())
        with patch("cogs.onboarding.store", store):
            await cog.on_member_join(member)
        self.assertEqual([call.args[0] for call in member.add_roles.await_args_list], [temp, new])
        member.send.assert_awaited_once()

    async def test_database_failure_does_not_grant_verification(self):
        interaction, member, guild = dm_interaction()
        store = SimpleNamespace(merge_member=AsyncMock(side_effect=StoreError("unavailable")))
        modal = NameModal(SimpleNamespace(prompt_birthday=AsyncMock()))
        modal.full_name._value = "Ada Lovelace"
        modal.gender._value = "female"
        with patch.object(config, "GUILD_ID", guild.id), patch("cogs.onboarding.store", store):
            await modal.on_submit(interaction)
        member.edit.assert_not_awaited()
        member.add_roles.assert_not_awaited()
        self.assertIn("couldn't save", interaction.followup.send.await_args.args[0])

    async def test_nickname_failure_does_not_grant_verification(self):
        interaction, member, guild = dm_interaction()
        response = SimpleNamespace(status=403, reason="Forbidden")
        member.edit.side_effect = discord.Forbidden(response, "permission denied")
        store = SimpleNamespace(merge_member=AsyncMock())
        modal = NameModal(SimpleNamespace(prompt_birthday=AsyncMock()))
        modal.full_name._value = "Ada Lovelace"
        modal.gender._value = "female"
        with patch.object(config, "GUILD_ID", guild.id), patch("cogs.onboarding.store", store):
            await modal.on_submit(interaction)
        self.assertEqual(store.merge_member.await_count, 1)
        member.add_roles.assert_not_awaited()
        self.assertIn("couldn't change", interaction.followup.send.await_args.args[0])

    async def test_invalid_gender_is_rejected(self):
        interaction, member, _ = dm_interaction()
        modal = NameModal(None)
        modal.full_name._value = "Ada Lovelace"
        modal.gender._value = "unknown"
        await modal.on_submit(interaction)
        member.edit.assert_not_awaited()
        self.assertIn("Male or Female", interaction.response.send_message.await_args.args[0])

    async def test_recordsync_fetches_uncached_member_then_edits_it(self):
        interaction, member, guild = dm_interaction(cached=False)
        store = SimpleNamespace(merge_member=AsyncMock())
        modal = CollectNameModal(member.id, store)
        modal.full_name._value = "Grace Hopper"

        with patch.object(config, "GUILD_ID", guild.id):
            await modal.on_submit(interaction)

        guild.fetch_member.assert_awaited_once_with(member.id)
        member.edit.assert_awaited_once_with(
            nick=cursive_nickname("Grace Hopper"),
            reason="Record sync: real name collected via DM",
        )
        store.merge_member.assert_awaited_once()
        self.assertEqual(store.merge_member.await_args.args[1]["real_name"], "Grace Hopper")
        self.assertIn("nickname is set", interaction.followup.send.await_args.args[0])

    async def test_missing_server_member_gets_a_reply_and_no_write(self):
        interaction, _, guild = dm_interaction(cached=False)
        guild.fetch_member.return_value = None
        store = SimpleNamespace(merge_member=AsyncMock())
        modal = CollectNameModal(42, store)
        modal.full_name._value = "Ada Lovelace"

        with patch.object(config, "GUILD_ID", guild.id):
            await modal.on_submit(interaction)

        store.merge_member.assert_not_awaited()
        self.assertIn("couldn't find you", interaction.followup.send.await_args.args[0])

    async def test_resolver_uses_configured_guild_for_dm(self):
        interaction, member, guild = dm_interaction()
        with patch.object(config, "GUILD_ID", guild.id):
            self.assertIs(await member_for_interaction(interaction), member)

    async def test_old_name_button_opens_updated_onboarding_form(self):
        cog = SimpleNamespace()
        interaction = SimpleNamespace(
            client=SimpleNamespace(get_cog=Mock(return_value=cog)),
            response=SimpleNamespace(send_modal=AsyncMock()),
        )
        view = CollectNameView(42)
        await view.set_name.callback(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, NameModal)
        self.assertFalse(modal.birthday.required)


if __name__ == "__main__":
    unittest.main()
