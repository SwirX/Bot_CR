"""Bot operator console — status, restart, update (bot admins only).

Handy for the club's on-call operatives: `/bot status` shows how the running
process is doing, `/bot restart` reloads the whole bot in place, and
`/bot update` pulls the latest `nightly` from GitHub and restarts — no SSH
needed. All commands are gated by :func:`cogs._perms.is_bot_admin`.

Restart strategy: the bot runs under systemd with ``NoNewPrivileges=true``, so
it can't `sudo systemctl restart` itself. Instead ``restart`` and ``update``
reply first and then ``os.execv`` back into ``BOT.py``: the same PID keeps
running (systemd never notices), every cog and the KeepAlive Flask server
re-initialise, and the whole process starts fresh — equivalent to a service
restart without any privilege escalation.
"""

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import discord
from discord.ext import commands

from cogs._perms import is_bot_admin

LOG = logging.getLogger("bot.admin")

ROOT = Path(__file__).resolve().parent.parent
_START = time.monotonic()


def _fmt_uptime(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _git_head() -> str:
    """Current branch + short commit, or 'unknown' if git isn't available."""
    try:
        rev = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "-C", str(ROOT), "branch", "--show-current"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return f"{branch}@{rev}" if branch and rev else "unknown"
    except Exception:  # noqa: BLE001 - status should never raise
        return "unknown"


def _relaunch() -> None:
    """Replace this process with a fresh copy of BOT.py (same PID)."""
    os.execv(sys.executable, [sys.executable, str(ROOT / "BOT.py")])


class BotAdmin(commands.Cog):
    """Operator console for the bot itself (bot admins only)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    async def _require_admin(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send("⛔ Run this inside the server.")
            return False
        if not is_bot_admin(ctx.author):
            await ctx.send("⛔ Bot admins only.")
            return False
        return True

    @commands.hybrid_group(
        name="bot",
        description="Bot operator commands (status, restart, update).",
        invoke_without_command=True,
    )
    async def bot(self, ctx: commands.Context):
        """`bot status` / `bot restart` / `bot update` — what this console does."""
        if not await self._require_admin(ctx):
            return
        await ctx.send("🛠️ `bot status` — health | `bot restart` — reload process | "
                       "`bot update` — pull latest nightly + restart")

    @bot.command(name="status", description="Show bot uptime, build, and command counts.")
    @commands.guild_only()
    async def status(self, ctx: commands.Context):
        if not await self._require_admin(ctx):
            return
        try:
            await ctx.defer()
        except discord.HTTPException:
            pass
        head = await asyncio.to_thread(_git_head)
        voice = len([v for v in self.bot.voice_clients if v.is_connected()])
        embed = discord.Embed(title="🤖 Bot status", color=0x2ECC71)
        embed.add_field(name="Uptime",
                        value=_fmt_uptime(int(time.monotonic() - _START)))
        embed.add_field(name="Latency",
                        value=f"{self.bot.latency * 1000:.0f} ms")
        embed.add_field(name="Build", value=f"`{head}`")
        embed.add_field(name="Cogs", value=str(len(self.bot.cogs)))
        embed.add_field(name="Commands", value=str(len(self.bot.commands)))
        embed.add_field(name="Slash", value=str(len(self.bot.tree.get_commands())))
        embed.add_field(name="Voice connections", value=str(voice))
        await ctx.send(embed=embed)

    @bot.command(name="restart", description="Restart the bot process (reloads all cogs).")
    @commands.guild_only()
    async def restart(self, ctx: commands.Context):
        if not await self._require_admin(ctx):
            return
        await ctx.send("🔄 Restarting… I'll be back in a few seconds.")
        LOG.info("Restart requested by %s (%s)", ctx.author, ctx.guild)
        _relaunch()

    @bot.command(name="update", description="Pull the latest nightly from GitHub and restart.")
    @commands.guild_only()
    async def update(self, ctx: commands.Context):
        if not await self._require_admin(ctx):
            return
        await ctx.defer()
        proc = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(ROOT), "pull", "--ff-only", "origin", "nightly"],
            capture_output=True, text=True, timeout=90,
        )
        if proc.returncode != 0:
            await ctx.send(f"⚠️ Pull failed:\n```{proc.stderr.strip()[:1500] or 'unknown error'}```")
            return
        summary = (proc.stdout.strip().splitlines() or ["already up to date."])[-1]
        await ctx.send(f"✅ `{summary}` — restarting…")
        LOG.info("Update applied by %s: %s", ctx.author, proc.stdout.strip())
        _relaunch()


async def setup(bot: commands.Bot):
    await bot.add_cog(BotAdmin(bot))