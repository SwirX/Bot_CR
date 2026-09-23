import logging
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

import config
from data.store import store
from data.store import StoreError
from data.levels import level_from_xp, xp_for_level, xp_progress
from cogs._ui import PaginatorView
from cogs._perms import mod_perms

LOG = logging.getLogger("bot.engagement")

tz = ZoneInfo("Africa/Casablanca")


def today_str() -> str:
    return datetime.now(tz).strftime("%Y-%m-%d")


def cap_grant(granted_today: int, cap: int, amount: int) -> int:
    """Whole XP still allowed by the daily voice cap; 0 when exhausted."""
    if granted_today >= cap or amount <= 0:
        return 0
    return min(amount, cap - granted_today)


class Engagement(commands.Cog):
    """XP & levels plus daily challenges, all Appwrite-backed."""

    def __init__(self, bot):
        self.bot = bot
        self.xp_pending = {}       # user_id -> XP earned since last flush
        self.xp_base = {}          # user_id -> stored XP as of last read
        self.level_seen = {}       # user_id -> highest announced level
        self.name_cache = {}       # user_id -> username
        self.xp_cooldown = {}      # user_id -> last time XP was granted
        self.voice_sessions = {}   # user_id -> [monotonic start, accrued fraction, guild_id]
        self.voice_day_granted = {}  # user_id -> [date, voice XP granted that day]
        self._voice_per_second = config.VOICE_XP_PER_MINUTE / 60.0
        self.daily_posted = None   # last date the challenge was posted for
        self.flush_loop.start()

    # ── XP accumulation ────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        uid = message.author.id
        self.name_cache[uid] = message.author.name

        now = time.monotonic()
        if now - self.xp_cooldown.get(uid, 0.0) < config.XP_COOLDOWN_SECONDS:
            return
        self.xp_cooldown[uid] = now

        gain = random.randint(config.XP_MIN, config.XP_MAX)
        await self._grant_xp(uid, gain, name=message.author.name,
                             mention=message.author.mention,
                             channel=message.channel)

    # ── shared XP grant ───────────────────────────────────────
    async def _grant_xp(self, uid: int, amount: int, *, name: str,
                        mention: str, channel=None) -> None:
        """Credit XP into the pending bucket and announce any level-up."""
        if amount <= 0:
            return
        self.name_cache[uid] = name
        self.xp_pending[uid] = self.xp_pending.get(uid, 0) + amount
        base = self.xp_base.get(uid)
        if base is None:
            try:
                record = await store.get_member(uid)
            except StoreError:
                record = None
            base = int((record or {}).get("xp", 0))
            self.xp_base[uid] = base
        level = level_from_xp(base + self.xp_pending[uid])
        prev = self.level_seen.get(uid, level_from_xp(base))
        if level > prev:
            self.level_seen[uid] = level
            if channel is not None:
                await channel.send(f"🎉 {mention} reached **level {level}**! GG!")

    # ── voice-channel XP ──────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        now = time.monotonic()
        if before.channel != after.channel:
            await self._close_voice_session(member, before.channel, now)
        if after.channel is not None and member.id not in self.voice_sessions:
            self.voice_sessions[member.id] = [now, 0.0, member.guild.id]

    def _adopt_existing_voice_sessions(self):
        """Seed sessions for members already in voice when the bot comes up."""
        now = time.monotonic()
        for guild in self.bot.guilds:
            for channel in guild.voice_channels:
                for member in channel.members:
                    if member.bot or member.id in self.voice_sessions:
                        continue
                    self.voice_sessions[member.id] = [now, 0.0, guild.id]

    def _accrue_voice(self, uid: int, now: float) -> int:
        """Whole XP a session earned since its last credit; the fraction carries."""
        session = self.voice_sessions.get(uid)
        if session is None:
            return 0
        elapsed = now - session[0]
        if elapsed <= 0:
            return 0
        session[0] = now
        session[1] += elapsed * self._voice_per_second
        whole = int(session[1])
        session[1] -= whole
        return whole

    async def _credit_open_voice_sessions(self):
        """Accrue XP for everyone in voice (called once per flush tick)."""
        now = time.monotonic()
        for uid, session in list(self.voice_sessions.items()):
            whole = self._accrue_voice(uid, now)
            if whole <= 0:
                continue
            guild = self.bot.get_guild(session[2])
            member = guild.get_member(uid) if guild else None
            if member is None:
                continue
            channel = member.voice.channel if member.voice else None
            await self._grant_voice_xp(member, channel, whole)

    async def _close_voice_session(self, member, channel, now: float):
        """Grant the leftover whole XP when a member leaves or switches channels."""
        session = self.voice_sessions.pop(member.id, None)
        if session is None:
            return
        elapsed = now - session[0]
        if elapsed <= 0:
            return
        accrued = session[1] + elapsed * self._voice_per_second
        whole = int(accrued)
        if whole > 0:
            await self._grant_voice_xp(member, channel, whole)

    async def _grant_voice_xp(self, member, channel, amount: int):
        """Daily-capped voice XP, routed through the shared grant path."""
        today = today_str()
        entry = self.voice_day_granted.get(member.id)
        if entry is None or entry[0] != today:
            entry = [today, 0]
            self.voice_day_granted[member.id] = entry
        grant = cap_grant(entry[1], config.VOICE_XP_DAILY_CAP, amount)
        if grant <= 0:
            return
        entry[1] += grant
        text = None
        if channel is not None:
            text = discord.utils.get(member.guild.text_channels, name=channel.name)
        await self._grant_xp(member.id, grant, name=member.name,
                             mention=member.mention, channel=text)

    # ── flush ──────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        await self.bot.wait_until_ready()
        if not self.flush_loop.is_running():
            self.flush_loop.start()
        self._adopt_existing_voice_sessions()

    @tasks.loop(seconds=config.DASHBOARD_REFRESH_SECONDS)
    async def flush_loop(self):
        await self._credit_open_voice_sessions()
        await self.flush()
        await self.maybe_post_daily_challenge()

    async def flush(self):
        """Persist pending XP deltas and update the local XP bases."""
        deltas = {}
        for uid, amount in self.xp_pending.items():
            deltas[uid] = {
                "xp": amount,
                "_bootstrap": {"username": self.name_cache.get(uid, "")},
            }
        if not deltas:
            return
        try:
            await store.flush_member_activity(deltas)
        except StoreError as exc:
            LOG.error("XP flush failed: %s", exc)
            return
        for uid, amount in self.xp_pending.items():
            self.xp_base[uid] = self.xp_base.get(uid, 0) + amount
        self.xp_pending.clear()

    # ── daily challenge auto-post ──────────────────────────────
    async def maybe_post_daily_challenge(self):
        """Post today's challenge to the announcements channel (once per day)."""
        date = today_str()
        if self.daily_posted == date:
            return
        self.daily_posted = date
        try:
            challenge = await store.get_challenge(date)
        except StoreError:
            return
        if not challenge:
            return
        channel = discord.utils.get(
            self.bot.guilds[0].text_channels, name=config.CHANNEL_ANNOUNCEMENTS
        ) if self.bot.guilds else None
        if channel is None:
            return
        await channel.send(
            f"🔥 **Today's challenge ({date}):** {challenge.get('title', '')}"
            + (f"\n{challenge.get('description', '')}" if challenge.get("description") else "")
        )

    # ── XP commands ────────────────────────────────────────────
    @commands.hybrid_command(name="rank", description="Show your (or someone's) level and XP.")
    async def rank(self, ctx, member: discord.Member | None = None):
        member = member or ctx.author
        uid = member.id
        try:
            record = await store.get_member(uid)
        except StoreError:
            record = None
        xp = int((record or {}).get("xp", 0)) + self.xp_pending.get(uid, 0)
        level, into, need = xp_progress(xp)
        filled = int(10 * into / need) if need else 10
        # White empties read as "not filled yet" on dark and light themes;
        # black squares were invisible on the dark sidebar.
        bar = "🟩" * filled + "⬜" * (10 - filled)
        percent = int(100 * into / need) if need else 100
        embed = discord.Embed(
            title=f"📈 {member.display_name} — Level {level}",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="XP",
            value=f"{xp:,} XP total · {into:,}/{need:,} to level {level + 1}",
        )
        embed.add_field(name="Progress", value=f"{bar} {percent}%", inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="leaderboard", description="Server XP leaderboard (paginated).")
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def leaderboard(self, ctx):
        try:
            members = await store.list_members(limit=100, order_by="xp")
        except StoreError:
            members = []
        rows = []
        for i, record in enumerate(members, start=1):
            uid = int(record.get("user_id") or 0)
            xp = int(record.get("xp", 0)) + self.xp_pending.get(uid, 0)
            name = (
                record.get("display_name")
                or record.get("username")
                or (self.bot.get_user(uid).name if self.bot.get_user(uid) else "???")
            )
            rows.append(f"`{i:>2}.` **{name}** — level {level_from_xp(xp)} ({xp} XP)")
        if not rows:
            await ctx.send("🏆 No XP recorded yet — start chatting!")
            return
        page_size = 10
        pages = []
        total_pages = (len(rows) + page_size - 1) // page_size
        for p in range(total_pages):
            embed = discord.Embed(
                title="🏆 Leaderboard",
                description="\n".join(rows[p * page_size:(p + 1) * page_size]),
                color=discord.Color.gold(),
            )
            embed.set_footer(text=f"Page {p + 1}/{total_pages}")
            pages.append(embed)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await ctx.send(embed=pages[0], view=PaginatorView(pages, user=ctx.author))

    # ── daily challenge commands ───────────────────────────────
    @commands.hybrid_group(name="challenge", description="Daily challenges.")
    async def challenge(self, ctx):
        await self.challenge_today(ctx)

    @challenge.command(name="today", description="Show today's challenge.")
    async def challenge_today(self, ctx):
        try:
            record = await store.get_challenge(today_str())
        except StoreError:
            record = None
        if not record:
            await ctx.send("📭 No challenge set for today yet.")
            return
        claimed = record.get("claimed") or []
        names = []
        for cid in claimed[:10]:
            member = ctx.guild.get_member(int(cid))
            names.append(member.display_name if member else f"<@{cid}>")
        message = f"🔥 **Today's challenge:** {record.get('title', '')}"
        if record.get("description"):
            message += f"\n{record['description']}"
        message += f"\n✅ Claimed by: {', '.join(names) if names else 'no one yet'}"
        await ctx.send(message)

    @challenge.command(name="set", description="Set today's challenge (staff).")
    @mod_perms(manage_messages=True)
    async def challenge_set(self, ctx, title: str = "", description: str = ""):
        """Slash invocation opens a modal form; prefix keeps the inline path."""
        if ctx.interaction is not None:
            await ctx.interaction.response.send_modal(ChallengeSetModal(
                self, title=title, description=description))
            return
        await self._save_challenge(
            author_name=ctx.author.display_name, title=title,
            description=description, respond=ctx.send)

    async def _save_challenge(self, *, author_name: str, title: str,
                              description: str, respond) -> None:
        """Shared core for /challenge set and its modal form."""
        if not title.strip():
            await respond("⚠️ A challenge needs a title.")
            return
        await store.save_challenge(
            today_str(),
            title=title.strip(),
            description=description.strip(),
            created_by=author_name,
        )
        await respond(f"✅ Today's challenge set: **{title.strip()}**")

    @challenge.command(name="claim", description="Claim today's challenge (once).")
    async def challenge_claim(self, ctx):
        try:
            claimed = await store.claim_challenge(today_str(), ctx.author.id)
        except StoreError as exc:
            await ctx.send(f"⚠️ Could not claim: {exc}")
            return
        await ctx.send(
            "🎉 Challenge claimed! Nice work."
            if claimed
            else "⚠️ You already claimed today's challenge!"
        )

    @challenge.command(name="history", description="The last few challenges.")
    async def challenge_history(self, ctx):
        try:
            recent = await store.list_recent_challenges(limit=7)
        except StoreError:
            recent = []
        if not recent:
            await ctx.send("No challenges recorded yet.")
            return
        lines = [
            f"**{r.get('date', '?')}** — {r.get('title', '')}"
            f" (claimed by {len(r.get('claimed') or [])})"
            for r in recent
        ]
        await ctx.send("🗓️ **Recent challenges**\n" + "\n".join(lines))


class ChallengeSetModal(discord.ui.Modal):
    """Pop-up form for /challenge set — pre-filled from slash args."""

    def __init__(self, cog: "Engagement", *, title: str = "", description: str = ""):
        super().__init__(title="🔥 Today's challenge")
        self.cog = cog
        self.title_input = discord.ui.TextInput(
            label="Title", placeholder="e.g. Build a line follower", max_length=256,
            default=title or None, required=True)
        self.desc_input = discord.ui.TextInput(
            label="Description", style=discord.TextStyle.paragraph,
            max_length=2048, default=description or None, required=False)
        self.add_item(self.title_input)
        self.add_item(self.desc_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.cog._save_challenge(
            author_name=interaction.user.display_name,
            title=self.title_input.value or "",
            description=self.desc_input.value or "",
            respond=lambda text: interaction.followup.send(text),
        )


async def setup(bot):
    await bot.add_cog(Engagement(bot))