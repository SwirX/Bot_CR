"""Resumable discovery of messages missed before startup or during downtime."""

import json
import logging
from datetime import timedelta

import discord

from data.store import StoreError, store
from cogs.message_expiry import MEMBERS, public_channel

LOG = logging.getLogger("bot.message_expiry")
CURSORS = "message_expiry_history_cursors"


async def scan_guild(cog, guild):
    try:
        raw = await store.get_setting(CURSORS)
        cursors = json.loads(raw) if raw else {}
        if not isinstance(cursors, dict):
            raise ValueError("expected a cursor object")
    except (StoreError, ValueError) as exc:
        LOG.error("Cannot load message expiry scan progress: %s", exc)
        return
    for channel in guild.channels:
        if not public_channel(channel):
            continue
        key = str(channel.id)
        after = discord.Object(id=int(cursors[key])) if key in cursors else None
        batch = []
        try:
            async for message in channel.history(limit=None, after=after, oldest_first=True):
                batch.append(message)
                if len(batch) >= 100:
                    if not await save_batch(cog, cursors, channel, batch):
                        return
                    batch = []
            if batch and not await save_batch(cog, cursors, channel, batch):
                return
        except discord.Forbidden:
            LOG.warning("Cannot scan message history in channel %s: missing permissions", channel.id)
        except discord.HTTPException as exc:
            LOG.warning("Cannot scan message history in channel %s: %s", channel.id, exc)


async def save_batch(cog, cursors, channel, messages):
    async with cog.lock:
        if not await cog.load():
            return False
        for message in messages:
            if message.author.id in MEMBERS and not message.author.bot and not message.is_system():
                cog.pending[str(message.id)] = {
                    "channel": channel.id,
                    "due": (message.created_at + timedelta(hours=24)).timestamp(),
                }
                cog.dirty = True
        await cog.save()
        if cog.dirty:
            return False
        updated = dict(cursors)
        updated[str(channel.id)] = str(messages[-1].id)
        try:
            await store.set_setting(CURSORS, json.dumps(updated))
        except StoreError as exc:
            LOG.error("Cannot save message expiry scan progress: %s", exc)
            return False
        cursors.update(updated)
        return True
