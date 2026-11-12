import asyncio
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import discord

from cogs._expiry_history import save_batch
from cogs.message_expiry import MEMBERS, MessageExpiry


class ExpiryHistoryTests(IsolatedAsyncioTestCase):
    async def test_saves_queue_before_advancing_cursor(self):
        cog = MessageExpiry.__new__(MessageExpiry)
        cog.lock = asyncio.Lock()
        cog.loaded = True
        cog.dirty = False
        cog.pending = {}
        channel = SimpleNamespace(id=22)
        message = SimpleNamespace(id=33, author=SimpleNamespace(id=next(iter(MEMBERS)), bot=False),
                                  created_at=discord.utils.utcnow(), is_system=lambda: False)
        cursors = {}
        with patch("data.store.store.set_setting", AsyncMock()) as save:
            self.assertTrue(await save_batch(cog, cursors, channel, [message]))
        self.assertEqual(save.await_count, 2)
        self.assertIn("33", json.loads(save.await_args_list[0].args[1]))
        self.assertEqual(cursors, {"22": "33"})

    async def test_failed_queue_write_does_not_advance_cursor(self):
        from data.store import StoreError
        cog = MessageExpiry.__new__(MessageExpiry)
        cog.lock = asyncio.Lock()
        cog.loaded = True
        cog.dirty = False
        cog.pending = {}
        message = SimpleNamespace(id=33, author=SimpleNamespace(id=next(iter(MEMBERS)), bot=False),
                                  created_at=discord.utils.utcnow(), is_system=lambda: False)
        cursors = {}
        with patch("data.store.store.set_setting", AsyncMock(side_effect=StoreError("offline"))):
            self.assertFalse(await save_batch(cog, cursors, SimpleNamespace(id=22), [message]))
        self.assertEqual(cursors, {})
