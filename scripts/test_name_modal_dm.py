"""DM name forms must edit a guild member, not the DM's User object."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("APPWRITE_API_KEY", "y")
os.environ.setdefault("APPWRITE_PROJECT_ID", "robotics-ops")
os.environ.setdefault("APPWRITE_DATABASE_ID", "robotics_hub")

import config  # noqa: E402
from cogs._member_identity import member_for_interaction  # noqa: E402
from cogs.onboarding import NameModal, cursive_nickname  # noqa: E402
from cogs.recordsync import CollectNameModal  # noqa: E402


def dm_interaction(*, cached=True):
    user = SimpleNamespace(id=42, display_name="Original")
    member = SimpleNamespace(
        id=42, display_name="Original", roles=[], edit=AsyncMock(),
        guild=None,
    )
    guild = SimpleNamespace(
        id=123, roles=[], get_member=lambda _id: member if cached else None,
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
    async def test_onboarding_edits_member_and_saves_name(self):
        interaction, member, guild = dm_interaction()
        store = SimpleNamespace(merge_member=AsyncMock(),
                                maybe_auto_link_club_member=AsyncMock())
        cog = SimpleNamespace(prompt_birthday=AsyncMock())
        modal = NameModal(cog)
        modal.full_name._value = "Ada Lovelace"

        with patch.object(config, "GUILD_ID", guild.id), patch("cogs.onboarding.store", store):
            await modal.on_submit(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        member.edit.assert_awaited_once_with(
            nick=cursive_nickname("Ada Lovelace"),
            reason="Onboarding: real name → cursive nickname",
        )
        store.merge_member.assert_awaited_once()
        self.assertEqual(store.merge_member.await_args.args[0], member.id)
        self.assertEqual(store.merge_member.await_args.args[1]["real_name"], "Ada Lovelace")
        cog.prompt_birthday.assert_awaited_once_with(member)
        self.assertIn("Verified", interaction.followup.send.await_args.args[0])

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


if __name__ == "__main__":
    unittest.main()
