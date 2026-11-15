import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

import config
from cogs import _meetings
from cogs.meetings import Meetings


class MeetingInvitesTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_members_audience_uses_role_id(self):
        role = SimpleNamespace(id=config.ROLE_NEW_MEMBER_ID, name="New Member", managed=False)
        other = SimpleNamespace(id=9, name="Other", managed=False)
        def member(uid, roles, bot=False):
            return SimpleNamespace(id=uid, roles=roles, bot=bot, display_name=str(uid))
        guild = SimpleNamespace(members=[member(1, [role]), member(2, [other]), member(3, [role], True)])
        result = await _meetings.resolve_audience(guild, _meetings.normalise_scope("New members"))
        self.assertEqual([person.id for person in result], [1])

    async def test_new_members_excludes_old_members_and_cell_staff(self):
        def role(uid, name):
            return SimpleNamespace(id=uid, name=name, managed=False)
        new = role(config.ROLE_NEW_MEMBER_ID, "New Member")
        old = role(1333538424724127825, "Old Member")
        staff = role(5, "Member of IT Unit")
        chief = role(6, "Head of IT Unit")
        def member(uid, roles):
            return SimpleNamespace(id=uid, roles=roles, bot=False, display_name=str(uid))
        guild = SimpleNamespace(members=[member(1, [new]), member(2, [new, old]),
                                         member(3, [new, staff]), member(4, [new, chief])])
        result = await _meetings.resolve_audience(guild, "new_members")
        self.assertEqual([person.id for person in result], [1])

    async def test_custom_invitation_contains_message_and_link(self):
        person = SimpleNamespace(send=AsyncMock())
        channel = SimpleNamespace(guild=SimpleNamespace(id=123), id=456)
        await Meetings(SimpleNamespace())._invite_audience(channel, [person], "Please bring your notes")
        args, kwargs = person.send.await_args
        self.assertEqual(args[0], "Please bring your notes\n\nJoin here: https://discord.com/channels/123/456")
        self.assertEqual(kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())

    async def test_end_defers_before_database_access(self):
        response = SimpleNamespace(is_done=lambda: False, defer=AsyncMock())
        ctx = SimpleNamespace(interaction=SimpleNamespace(response=response), send=AsyncMock())
        cog = Meetings(SimpleNamespace())
        async def live():
            response.defer.assert_awaited_once_with(ephemeral=True)
            return None
        cog._live_meeting = live
        await Meetings.meeting_end.callback(cog, ctx)
        self.assertIn("No meeting", ctx.send.await_args.args[0])

    async def test_new_member_meeting_grants_role_and_organizer(self):
        from unittest.mock import patch
        from scripts.test_meeting_cog import _Ctx, _FakeStore, _Member, _Role, build_cog
        organizer = _Member("Organizer", 10, "Manager")
        attendee = _Member("Attendee", 11)
        new_role = _Role("New Member", config.ROLE_NEW_MEMBER_ID)
        attendee.roles.append(new_role)
        store = _FakeStore()
        cog = build_cog(store, [organizer, attendee])
        channel = cog._test_channel
        channel.guild.get_role = lambda uid: new_role if uid == new_role.id else None
        cog._claim_voice = AsyncMock()
        cog._invite_audience = AsyncMock()
        cog._watch_empty = lambda _: None
        ctx = _Ctx(organizer, channel.guild)
        with patch("cogs.meetings.store", store):
            await cog._start_meeting(ctx, channel, audience="new members", message="Hello")
        self.assertEqual(store.created[0]["expected"], ["11"])
        self.assertEqual(store.created[0]["visitors"], ["10"])
        self.assertIn(attendee.id, [target for target, _ in channel.writes])
        self.assertIn(organizer.id, [target for target, _ in channel.writes])
        cog._invite_audience.assert_awaited_once_with(channel, [attendee], "Hello")
