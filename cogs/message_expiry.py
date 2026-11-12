"""Delete selected members' new server messages after 24 hours."""

import asyncio
import json
import logging
from datetime import timedelta

import discord
from discord.ext import commands, tasks

import config
from data.store import StoreError, store

LOG = logging.getLogger("bot.message_expiry")
SETTING = "message_expiry_queue"
MEMBERS = frozenset({1545453299015094283, 1410407463152386071})
VERIFIED_ROLE_ID = 1333113498669940905
BUREAU_CATEGORY_ID = 1336709394926141532


def public_channel(channel):
    if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
        return False
    if channel.category_id == BUREAU_CATEGORY_ID:
        return False
    verified = channel.guild.get_role(VERIFIED_ROLE_ID)
    return (channel.permissions_for(channel.guild.default_role).view_channel
            or bool(verified and channel.permissions_for(verified).view_channel))


class MessageExpiry(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pending = {}
        self.loaded = False
        self.dirty = False
        self.lock = asyncio.Lock()
        self.poll.start()

    def cog_unload(self):
        self.poll.cancel()

    async def load(self):
        if self.loaded:
            return True
        try:
            raw = await store.get_setting(SETTING)
            saved = json.loads(raw) if raw else {}
            if not isinstance(saved, dict):
                raise ValueError("expected a queue object")
        except (StoreError, ValueError) as exc:
            LOG.error("Cannot load message expiry queue: %s", exc)
            return False
        saved.update(self.pending)
        self.pending = saved
        self.loaded = True
        return True

    async def save(self):
        if not self.loaded or not self.dirty:
            return
        try:
            await store.set_setting(SETTING, json.dumps(self.pending))
        except StoreError as exc:
            LOG.error("Cannot save message expiry queue: %s", exc)
        else:
            self.dirty = False

    @commands.Cog.listener()
    async def on_message(self, message):
        if (not message.guild or message.author.bot or message.author.id not in MEMBERS
                or (config.GUILD_ID and message.guild.id != config.GUILD_ID)
                or message.is_system() or not public_channel(message.channel)):
            return
        async with self.lock:
            await self.load()
            self.pending[str(message.id)] = {
                "channel": message.channel.id,
                "due": (message.created_at + timedelta(hours=24)).timestamp(),
            }
            self.dirty = True
            await self.save()

    @tasks.loop(seconds=60)
    async def poll(self):
        async with self.lock:
            if not await self.load():
                return
            await self.save()
            now = discord.utils.utcnow().timestamp()
            for message_id, entry in list(self.pending.items()):
                if entry["due"] > now:
                    continue
                try:
                    channel = self.bot.get_channel(entry["channel"])
                    if channel is None:
                        channel = await self.bot.fetch_channel(entry["channel"])
                    if not public_channel(channel):
                        del self.pending[message_id]
                        self.dirty = True
                        continue
                    await channel.get_partial_message(int(message_id)).delete()
                except discord.NotFound:
                    pass
                except (discord.HTTPException, AttributeError) as exc:
                    LOG.warning("Could not delete expired message %s: %s", message_id, exc)
                    continue
                del self.pending[message_id]
                self.dirty = True
            await self.save()

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(MessageExpiry(bot))
