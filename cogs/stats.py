import logging
import time

import discord
from discord.ext import commands, tasks

import config
from data import store

LOG = logging.getLogger("bot.stats")

ACTIVE_STATUSES = (discord.Status.online, discord.Status.idle, discord.Status.dnd)


def fmt_seconds(seconds: float) -> str:
    """Format a duration as 00h 00m 00s."""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}h {minutes:02d}m {secs:02d}s"


class Stats(commands.Cog):
    """Unified stats tracking: messages, voice time, online presence.

    High-frequency events are accumulated in memory and flushed to the Appwrite
    store every ``config.DASHBOARD_REFRESH_SECONDS`` seconds, so per-message /
    per-second writes never reach the database. The dashboard reads the
    persisted counters instead of scanning channel history.
    """

    def __init__(self, bot):
        self.bot = bot
        # Pending accumulators (since the last flush).
        self.total_messages = 0          # global
        self.total_voice_seconds = 0.0   # global
        self.message_deltas = {}         # user_id -> messages since last flush
        self.voice_seconds = {}          # user_id -> seconds since last flush
        self.voice_join = {}             # user_id -> monotonic timestamp of join

    # ── events ─────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        self.total_messages += 1
        self.message_deltas[message.author.id] = self.message_deltas.get(message.author.id, 0) + 1

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        now = time.monotonic()

        # Close the previous segment when the channel changed or the user left.
        if before.channel != after.channel and member.id in self.voice_join:
            start = self.voice_join.pop(member.id)
            elapsed = now - start
            self.total_voice_seconds += elapsed
            self.voice_seconds[member.id] = self.voice_seconds.get(member.id, 0.0) + elapsed

        # Open a new segment whenever the user is in a voice channel.
        if after.channel is not None and member.id not in self.voice_join:
            self.voice_join[member.id] = now

    @commands.Cog.listener()
    async def on_ready(self):
        await self.bot.wait_until_ready()
        # Reconnect guard: the loop may already be running across reconnects.
        if not self.flush_loop.is_running():
            self.flush_loop.start()

    # ── flushing ───────────────────────────────────────────────
    @tasks.loop(seconds=config.DASHBOARD_REFRESH_SECONDS)
    async def flush_loop(self):
        await self.flush()

    async def flush(self):
        """Persist accumulated counters and per-member deltas in one pass."""
        activity, messages, voice_seconds = self._drain()
        try:
            if activity:
                await store.flush_member_activity(activity)
            if messages or voice_seconds:
                await store.bump_counters(messages=messages, voice_seconds=voice_seconds)
        except store.StoreError as exc:
            LOG.error("Stats flush failed: %s", exc)

    def _drain(self):
        activity = {}
        for uid, n in self.message_deltas.items():
            activity[uid] = {"messages": n}
        for uid, secs in self.voice_seconds.items():
            activity.setdefault(uid, {})["voice_seconds"] = round(secs, 1)
        self.message_deltas.clear()
        self.voice_seconds.clear()
        total_messages, self.total_messages = self.total_messages, 0
        total_voice, self.total_voice_seconds = self.total_voice_seconds, 0.0
        return activity, total_messages, total_voice

    # ── commands ───────────────────────────────────────────────
    @commands.command()
    async def total_messages(self, ctx):
        """Total messages sent in the server (persisted + pending)."""
        counters = await store.get_counters()
        total = int(counters.get("total_messages", 0)) + self.total_messages
        await ctx.send(f"Total messages sent in the server: {total}")

    @commands.command()
    async def total_voice_time(self, ctx):
        """Total voice time spent in the server."""
        counters = await store.get_counters()
        total = float(counters.get("total_voice_seconds", 0.0)) + self.total_voice_seconds
        await ctx.send(f"Total voice time in the server: {fmt_seconds(total)}")

    @commands.command()
    async def current_voice_time(self, ctx):
        """Time everyone has spent in the voice channel you are in, this session."""
        if not ctx.author.voice:
            await ctx.send("You are not in a voice channel!")
            return
        channel = ctx.author.voice.channel
        now = time.monotonic()
        total = 0.0
        for uid, start in self.voice_join.items():
            member = ctx.guild.get_member(uid)
            if member and member.voice and member.voice.channel == channel:
                total += now - start
        await ctx.send(
            f"Total time spent in {channel.name} so far: {fmt_seconds(total)}"
        )

    @commands.command()
    async def online_members(self, ctx):
        """Number of members currently online/idle/dnd."""
        online = sum(1 for m in ctx.guild.members if m.status in ACTIVE_STATUSES)
        await ctx.send(f"Number of online members: {online}")


async def setup(bot):
    await bot.add_cog(Stats(bot))