import logging
from datetime import timedelta

import discord
from discord.ext import commands

from data import store
from data.store import StoreError

LOG = logging.getLogger("bot.moderation")


class Moderation(commands.Cog):
    """Kick, ban, timeout, warn — every action lands in the Appwrite modlog."""

    def __init__(self, bot):
        self.bot = bot

    # ── helpers ────────────────────────────────────────────────
    async def _can_target(self, ctx: commands.Context, member: discord.Member) -> bool:
        """Refuse obviously invalid or hierarchy-blocked targets."""
        if member == ctx.author:
            await ctx.send("⛔ You can't moderate yourself.")
            return False
        if member == self.bot.user:
            await ctx.send("Nice try. I'm not doing that to myself. 🤖")
            return False
        if ctx.author != ctx.guild.owner and member.top_role >= ctx.author.top_role:
            await ctx.send(f"⛔ **{member.display_name}** has a role equal to or higher than yours.")
            return False
        if member.top_role >= ctx.guild.me.top_role:
            await ctx.send("⛔ My role isn't high enough to moderate that member.")
            return False
        return True

    async def _log(self, ctx: commands.Context, action: str, member: discord.Member, reason: str):
        try:
            await store.log_moderation(
                action=action,
                target_id=member.id,
                target_name=member.display_name,
                moderator_id=ctx.author.id,
                moderator_name=ctx.author.display_name,
                reason=reason,
            )
        except StoreError as exc:
            LOG.error("Modlog write failed: %s", exc)

    # ── purge (from the legacy suite) ──────────────────────────
    @commands.hybrid_command(name="del", description="Delete the last N messages (1–100).")
    @commands.has_permissions(manage_messages=True)
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def delete_messages(self, ctx, number: int):
        """Delete the last N messages in this channel plus the command itself."""
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send("This command can only be used in text channels.")
            return

        number = max(1, min(number, 100))
        try:
            deleted = await ctx.channel.purge(limit=number, before=ctx.message)
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
            await ctx.send(f"Deleted {len(deleted)} message(s) in #{ctx.channel.name}.", delete_after=3)
        except discord.Forbidden:
            await ctx.send("I don't have permission to delete messages here.", delete_after=5)
        except discord.HTTPException as exc:
            await ctx.send(f"Failed to delete messages: {exc}", delete_after=5)

    # ── kick ───────────────────────────────────────────────────
    @commands.hybrid_command(name="kick", description="Kick a member from the server.")
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if not await self._can_target(ctx, member):
            return
        try:
            await member.kick(reason=f"{ctx.author.name}: {reason}")
        except discord.Forbidden:
            await ctx.send("⛔ I don't have permission to kick that member.")
            return
        await ctx.send(f"👢 Kicked **{member.display_name}** — {reason}")
        await self._log(ctx, "kick", member, reason)

    # ── ban / unban ────────────────────────────────────────────
    @commands.hybrid_command(name="ban", description="Ban a member from the server.")
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if not await self._can_target(ctx, member):
            return
        try:
            await member.ban(reason=f"{ctx.author.name}: {reason}", delete_message_days=0)
        except discord.Forbidden:
            await ctx.send("⛔ I don't have permission to ban that member.")
            return
        await ctx.send(f"🔨 Banned **{member.display_name}** — {reason}")
        await self._log(ctx, "ban", member, reason)

    @commands.hybrid_command(name="unban", description="Unban a user by their ID.")
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx, user_id: int, *, reason: str = "No reason provided"):
        try:
            await ctx.guild.unban(discord.Object(id=user_id), reason=f"{ctx.author.name}: {reason}")
        except discord.NotFound:
            await ctx.send("⚠️ That user isn't banned.")
            return
        await ctx.send(f"🔓 Unbanned <@{user_id}> — {reason}")

    # ── timeout ────────────────────────────────────────────────
    @commands.hybrid_command(name="timeout", description="Timeout a member (minutes).")
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(self, ctx, member: discord.Member, minutes: int, *, reason: str = "No reason provided"):
        if not await self._can_target(ctx, member):
            return
        minutes = max(1, min(minutes, 10080))  # Discord caps timeouts at 28 days
        try:
            await member.timeout(discord.utils.utcnow() + timedelta(minutes=minutes),
                                 reason=f"{ctx.author.name}: {reason}")
        except discord.Forbidden:
            await ctx.send("⛔ I don't have permission to timeout that member.")
            return
        await ctx.send(f"🔇 Timed out **{member.display_name}** for {minutes} min — {reason}")
        await self._log(ctx, "timeout", member, reason)

    @commands.hybrid_command(name="untimeout", description="Remove a member's timeout.")
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(self, ctx, member: discord.Member, *, reason: str = "Timeout lifted"):
        if not await self._can_target(ctx, member):
            return
        try:
            await member.timeout(None, reason=f"{ctx.author.name}: {reason}")
        except discord.Forbidden:
            await ctx.send("⛔ I don't have permission to modify that member's timeout.")
            return
        await ctx.send(f"🔓 Timeout lifted for **{member.display_name}**")
        await self._log(ctx, "untimeout", member, reason)

    # ── warn ───────────────────────────────────────────────────
    @commands.hybrid_command(name="warn", description="Warn a member (recorded in the modlog).")
    @commands.has_permissions(moderate_members=True)
    async def warn(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if member.bot:
            await ctx.send("⚠️ I don't track warnings for bots.")
            return
        try:
            await store.increment_member(member.id, "warnings", 1, bootstrap={"username": member.name})
        except StoreError as exc:
            LOG.error("Warn counter update failed: %s", exc)
        await ctx.send(f"⚠️ Warned **{member.display_name}** — {reason}")
        await self._log(ctx, "warn", member, reason)

    # ── modlog ─────────────────────────────────────────────────
    @commands.hybrid_command(name="modlog", description="Show recent moderation actions.")
    @commands.has_permissions(moderate_members=True)
    async def modlog(self, ctx, limit: int = 10):
        limit = max(1, min(limit, 25))
        try:
            entries = await store.list_modlog(limit=limit)
        except StoreError as exc:
            await ctx.send(f"⚠️ Could not read the modlog: {exc}")
            return
        if not entries:
            await ctx.send("📭 No moderation actions recorded yet.")
            return
        lines = []
        for e in entries:
            when = str(e.get("created_at", ""))[:16].replace("T", " ")
            target = e.get("target_name") or f"<@{e.get('target_id')}>"
            mod = e.get("moderator_name") or "staff"
            reason = e.get("reason") or ""
            lines.append(f"`{when}` **{e.get('action', '?')}** {target} — {reason} *(by {mod})*")
        await ctx.send("🛡️ **Recent moderation actions**\n" + "\n".join(lines))


async def setup(bot):
    await bot.add_cog(Moderation(bot))