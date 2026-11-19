import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs import _voice_mute as vm


class Member:
    def __init__(self, uid, channel, *, muted=False, bot=False):
        self.id, self.bot = uid, bot
        self.voice = SimpleNamespace(channel=channel, mute=muted)
        self.edit = AsyncMock(side_effect=self.update)

    async def update(self, *, mute, reason):
        self.voice.mute = mute


class VoiceMuteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = {}
        async def get(key):
            return self.saved.get(key)
        async def set_value(key, value):
            self.saved[key] = value
        self.store_patch = patch.object(vm, "store", SimpleNamespace(get_setting=get, set_setting=set_value))
        self.store_patch.start()
        self.admin_patch = patch.object(vm, "is_bot_admin", return_value=False)
        self.admin_patch.start()
        self.channel = SimpleNamespace(id=123, mention="<#123>", members=[],
                                       permissions_for=lambda _: SimpleNamespace(mute_members=True))
        self.author = SimpleNamespace(id=9, voice=SimpleNamespace(channel=self.channel))
        self.members = [Member(1, self.channel), Member(2, self.channel),
                        Member(3, self.channel, muted=True), Member(4, self.channel, bot=True)]
        self.channel.members = self.members
        self.guild = SimpleNamespace(me=object(), get_member=lambda uid: next((m for m in self.members if m.id == uid), None))
        self.ctx = SimpleNamespace(author=self.author, guild=self.guild, send=AsyncMock())

    async def asyncTearDown(self):
        self.admin_patch.stop()
        self.store_patch.stop()

    async def test_exclude_and_undo_leave_preexisting_mutes_untouched(self):
        await vm.mute_all(self.ctx, exclude=self.members[1])
        self.assertTrue(self.members[0].voice.mute)
        self.assertFalse(self.members[1].voice.mute)
        self.assertTrue(self.members[2].voice.mute)
        self.members[3].edit.assert_not_awaited()
        self.assertEqual(json.loads(self.saved[vm._key(self.channel)]), ["1"])
        await vm.undo_mute(self.ctx)
        self.assertFalse(self.members[0].voice.mute)
        self.assertTrue(self.members[2].voice.mute)
        self.assertEqual(json.loads(self.saved[vm._key(self.channel)]), [])

    async def test_repeated_batch_does_not_erase_undo(self):
        await vm.mute_all(self.ctx)
        before = self.saved[vm._key(self.channel)]
        await vm.mute_all(self.ctx)
        self.assertEqual(self.saved[vm._key(self.channel)], before)
        self.assertIn("Undo it first", self.ctx.send.await_args.args[0])

    async def test_no_permission_does_not_mute(self):
        self.channel.permissions_for = lambda _: SimpleNamespace(mute_members=False)
        await vm.mute_all(self.ctx)
        self.members[0].edit.assert_not_awaited()
        self.assertIn("need Mute Members", self.ctx.send.await_args.args[0])

    async def test_failed_unmute_stays_available_for_retry(self):
        await vm.mute_all(self.ctx)
        self.members[0].edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
        await vm.undo_mute(self.ctx)
        self.assertEqual(json.loads(self.saved[vm._key(self.channel)]), ["1"])
        self.assertFalse(self.members[1].voice.mute)
