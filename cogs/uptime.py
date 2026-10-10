"""Uptime monitoring for the Appwrite backend.

The incident this exists for: Appwrite's own Docker healthcheck curls
`/v1/health/version`, which is a static version string served by nginx and
never touches the database. During a 16-hour MongoDB outage the container was
reported "(healthy)" the entire time while every project route returned 500.

So "is the container up" is the wrong question. These checks ask whether the
API can actually serve an authenticated, project-scoped request -- which is
what breaks when the backing store is unreachable.

Three tiers, cheapest first:
  * ``/v1/health/version``  public; catches a dead proxy or container.
  * ``/v1/databases``       authenticated + project-scoped; catches the
    database being down. This is the check that would have caught the outage.
  * ``/v1/health``          authenticated readiness probe.

A failure must survive ``UPTIME_FAILURES_BEFORE_ALERT`` consecutive polls
before alerting, so a single slow response does not page anyone. Recovery is
always announced.

NOTE: this bot runs on the same OCI host as the backend it monitors. A
backend-only outage (the failure that prompted this) is detected fine. A
whole-host failure takes the monitor down with it, which is why the deadman
switch below exists -- an external service watches this process, so a monitor
that dies silently is still reported.
"""

import asyncio
import json
import logging
import time
from typing import NamedTuple

import aiohttp
import discord
from discord.ext import commands, tasks

import config

LOG = logging.getLogger("bot.uptime")

# Discord colours.
RED = discord.Color.red()
GREEN = discord.Color.green()
AMBER = discord.Color.orange()


class Probe(NamedTuple):
    """One monitoring tier."""

    key: str
    path: str
    auth: bool
    label: str


PROBES: tuple[Probe, ...] = (
    Probe("version", "/health/version", False, "Edge / container alive"),
    Probe("databases", "/databases", True, "Authenticated project request"),
    Probe("health", "/health", True, "Readiness"),
)


class Uptime(commands.Cog):
    """Polls the Appwrite API and alerts on sustained failure."""

    def __init__(self, bot):
        self.bot = bot
        self._consecutive_failures = 0
        self._down_since: float | None = None
        self._down_detail: str | None = None
        self._alerted = False
        self._last_error: str | None = None
        self._latency: float | None = None
        self._poll.start()
        self._deadman_loop.start()

    def cog_unload(self):
        self._poll.cancel()
        self._deadman_loop.cancel()

    # ── probing ──────────────────────────────────────────────
    async def _run_probe(self, session: aiohttp.ClientSession, probe: Probe) -> str | None:
        """Return None on success, or a human-readable reason for failure."""
        headers = {"X-Appwrite-Project": config.APPWRITE_PROJECT_ID}
        if probe.auth:
            headers["X-Appwrite-Key"] = config.APPWRITE_API_KEY

        started = time.monotonic()
        try:
            async with session.get(
                f"{config.APPWRITE_ENDPOINT}{probe.path}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=config.UPTIME_TIMEOUT),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    return f"HTTP {resp.status} on `{probe.path}` ({body[:120]})"
                if probe.key == "health":
                    # /health answers 200 even when a sub-check fails; the
                    # signal is in the body. Observed shape:
                    #   {"name":"http","ping":0,"status":"pass"}
                    # A failing sub-check reports status "fail" here, so a
                    # 200 alone is NOT a pass.
                    try:
                        payload = json.loads(body)
                    except ValueError:
                        return f"`/health` returned non-JSON: {body[:120]}"
                    if payload.get("status") != "pass":
                        return f"`/health` status={payload.get('status')!r}: {body[:120]}"
                elapsed = time.monotonic() - started
                if probe.key == "version":
                    self._latency = elapsed
                return None
        except asyncio.TimeoutError:
            return f"Timed out after {config.UPTIME_TIMEOUT}s on `{probe.path}`"
        except aiohttp.ClientError as exc:
            return f"Connection error on `{probe.path}`: {exc.__class__.__name__}: {exc}"

    async def _check(self) -> str | None:
        """Run every tier. Returns the first failure reason, or None if healthy."""
        timeout = aiohttp.ClientTimeout(total=config.UPTIME_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for probe in PROBES:
                reason = await self._run_probe(session, probe)
                if reason is not None:
                    return f"{probe.label} — {reason}"
        return None

    # ── alerting ─────────────────────────────────────────────
    def _recipients(self) -> list[discord.abc.Messageable]:
        """DM targets first, then the channel. Deduped, missing ones skipped."""
        targets: list[discord.abc.Messageable] = []
        seen: set[int] = set()

        for raw_id in sorted(config.UPTIME_ALERT_USER_IDS):
            try:
                user_id = int(raw_id)
            except (TypeError, ValueError):
                LOG.warning("Ignoring non-numeric UPTIME_ALERT_USER_IDS entry %r", raw_id)
                continue
            user = self.bot.get_user(user_id)
            if user is not None and user_id not in seen:
                seen.add(user_id)
                targets.append(user)

        channel = self._alert_channel()
        if channel is not None and channel.id not in seen:
            seen.add(channel.id)
            targets.append(channel)
        return targets

    def _alert_channel(self) -> discord.abc.Messageable | None:
        """Resolve the alerts channel by ID, falling back to name.

        ID first: the channel name carries emoji and punctuation that are easy
        to mistype and easy to break with a rename. The name lookup is only a
        safety net for when the ID stops resolving (channel deleted, or the
        bot removed from it).
        """
        if config.CHANNEL_UPTIME_ALERT_ID:
            channel = self.bot.get_channel(config.CHANNEL_UPTIME_ALERT_ID)
            if channel is not None:
                return channel
            LOG.warning(
                "Uptime alert channel ID %s not found (deleted, or the bot was "
                "removed?). Falling back to name lookup.",
                config.CHANNEL_UPTIME_ALERT_ID,
            )
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=config.CHANNEL_UPTIME_ALERT)
            if channel is not None:
                LOG.warning(
                    "Resolved uptime alert channel %r by NAME; set "
                    "CHANNEL_UPTIME_ALERT_ID to pin it.",
                    config.CHANNEL_UPTIME_ALERT,
                )
                return channel
        return None

    async def _notify(self, embed: discord.Embed) -> None:
        """Fan out to DMs and the channel. Never raises."""
        targets = self._recipients()
        if not targets:
            LOG.error(
                "No uptime alert targets resolved. Check CHANNEL_UPTIME_ALERT_ID "
                "(%s) and UPTIME_ALERT_USER_IDS; this alert was dropped.",
                config.CHANNEL_UPTIME_ALERT_ID,
            )
            return
        for target in targets:
            try:
                await target.send(embed=embed)
            except discord.HTTPException as exc:
                LOG.warning("Could not deliver uptime alert to %s: %s", target, exc)

    def _down_embed(self, duration: str, latency: str) -> discord.Embed:
        embed = discord.Embed(
            title="🔴 Appwrite backend is DOWN",
            color=RED,
        )
        embed.description = self._down_detail or "No detail available."
        embed.add_field(name="Down for", value=duration, inline=True)
        embed.add_field(name="Consecutive failures", value=str(self._consecutive_failures),
                        inline=True)
        embed.add_field(name="Last known latency", value=latency, inline=True)
        embed.add_field(name="Endpoint", value=f"`{config.APPWRITE_ENDPOINT}`", inline=False)
        embed.set_footer(
            text="Note: Appwrite's Docker healthcheck passes during this failure — "
                 "it only reads a static version string."
        )
        return embed

    def _up_embed(self, outage: str) -> discord.Embed:
        embed = discord.Embed(
            title="🟢 Appwrite backend is BACK UP",
            color=GREEN,
        )
        embed.add_field(name="Outage lasted", value=outage, inline=True)
        embed.add_field(name="Latency", value=self._latency_str(), inline=True)
        return embed

    def _latency_str(self) -> str:
        if self._latency is None:
            return "unknown"
        return f"{self._latency * 1000:.0f} ms"

    @staticmethod
    def _duration(seconds: float) -> str:
        parts: list[str] = []
        for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
            count, seconds = divmod(seconds, size)
            if count:
                parts.append(f"{count}{unit}")
        if not parts:
            return f"{seconds:.0f}s"
        return " ".join(parts)

    # ── main loop ────────────────────────────────────────────
    @tasks.loop(seconds=config.UPTIME_POLL_SECONDS)
    async def _poll(self):
        reason = await self._check()

        if reason is None:
            if self._alerted or self._consecutive_failures:
                # We had a problem and it just cleared.
                outage = "unknown"
                if self._down_since is not None:
                    outage = self._duration(time.monotonic() - self._down_since)
                await self._notify(self._up_embed(outage))
            self._consecutive_failures = 0
            self._down_since = None
            self._down_detail = None
            self._last_error = None
            self._alerted = False
            return

        # Failure path.
        self._last_error = reason
        if self._down_since is None:
            self._down_since = time.monotonic()
            LOG.warning("Uptime check failed: %s", reason)
        self._consecutive_failures += 1

        if (
            not self._alerted
            and self._consecutive_failures >= config.UPTIME_FAILURES_BEFORE_ALERT
        ):
            self._alerted = True
            duration = self._duration(time.monotonic() - self._down_since)
            LOG.error(
                "Appwrite DOWN for %s after %d consecutive failures: %s",
                duration, self._consecutive_failures, reason,
            )
            await self._notify(self._down_embed(duration, self._latency_str()))

    @_poll.before_loop
    async def _before_loop(self):
        # Don't fire a page the instant the process restarts.
        await self.bot.wait_until_ready()

    # ── deadman's switch ─────────────────────────────────────
    # A monitor that dies silently is worse than no monitor: you would keep
    # believing you were covered. The bot pings this URL every cycle; if the
    # ping stops arriving for DEADMAN_MISSED_POLLS, an external service
    # (UptimeRobot / healthchecks.io) alerts you that the monitor is gone.
    #
    # This is deliberately NOT self-hosting the check on the same host --
    # that would die with the thing it monitors.
    async def _deadman_ping(self) -> None:
        if not config.DEADMAN_URL:
            return
        try:
            timeout = aiohttp.ClientTimeout(total=config.UPTIME_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(config.DEADMAN_URL) as resp:
                    if resp.status >= 400:
                        LOG.warning("Deadman ping returned HTTP %s", resp.status)
        except asyncio.TimeoutError:
            LOG.warning("Deadman ping timed out")
        except aiohttp.ClientError as exc:
            LOG.warning("Deadman ping failed: %s", exc)

    @tasks.loop(seconds=config.DEADMAN_POLL_SECONDS)
    async def _deadman_loop(self):
        await self._deadman_ping()

    @_deadman_loop.before_loop
    async def _deadman_before_loop(self):
        await self.bot.wait_until_ready()

    @commands.command(name="uptime", help="Check Appwrite backend health on demand.")
    @commands.cooldown(1.0, 30.0, commands.BucketType.guild)
    async def uptime_command(self, ctx: commands.Context):
        reason = await self._check()
        if reason is None:
            embed = discord.Embed(title="🟢 Appwrite is healthy", color=GREEN)
            embed.add_field(name="Latency", value=self._latency_str(), inline=True)
        else:
            embed = discord.Embed(title="🔴 Appwrite is NOT responding", color=RED)
            embed.description = reason
            if self._consecutive_failures:
                embed.add_field(
                    name="Consecutive failures",
                    value=str(self._consecutive_failures), inline=True,
                )
        if ctx.interaction:
            await ctx.interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Uptime(bot))
