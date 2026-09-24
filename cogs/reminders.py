"""Timed DM reminders: ``/remind me in <span> <text>``.

Reminders are persisted in Appwrite's ``bot_settings`` sidecar (a JSON blob),
so they survive restarts — the bot relaunches on every ``/bot update``, and an
in-memory-only queue would silently lose everything scheduled.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

import config
from cogs._dates import parse_span
from data.store import store, StoreError

LOG = logging.getLogger("bot.reminders")

_SETTING_KEY = "reminders"   # bot_settings row holding the pending JSON blob
_MAX_PER_USER = 10


def _iso_at(seconds_from_now: int) -> str:
    """UTC ISO timestamp seconds_from_now out (lexicographic compare works)."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)
            ).isoformat(timespec="seconds")


def _short_id(uid: int) -> str:
    """Compact, human-typable id used by /remind cancel."""
    rel = int(time.monotonic() * 1000) & 0xFFFFF
    return f"{rel:x}{uid & 0xFF:x}"


def fmt_span(seconds: int) -> str:
    """'2h 30m 5s' for a seconds count (parts below the largest are included)."""
    parts: list[str] = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count}{unit}")
    return " ".join(parts) or "0s"


class Reminders(commands.Cog):
    """Appwrite-backed timed reminders delivered by DM."""

    def __init__(self, bot):
        self.bot = bot
        self._pending: list[dict] = []   # {id, user_id, due_at, text}
        self._loaded = False
        self._poll.start()

    @tasks.loop(seconds=config.REMINDER_POLL_SECONDS)
    async def _poll(self):
        """Deliver due reminders, then persist the trimmed queue."""
        if not self._loaded:
            self._loaded = True
            await self._load()
        now = _iso_at(0)
        due = [r for r in self._pending if r["due_at"] <= now]
        if not due:
            return
        for reminder in due:
            await self._fire(reminder)
        fired = {r["id"] for r in due}
        self._pending = [r for r in self._pending if r["id"] not in fired]
        await self._save()

    # ── persistence ──────────────────────────────────────────
    async def _load(self) -> None:
        try:
            raw = await store.get_setting(_SETTING_KEY)
        except StoreError as exc:
            LOG.warning("Could not load reminders: %s", exc)
            return
        try:
            rows = json.loads(raw) if raw else []
        except ValueError:
            LOG.warning("Ignoring corrupt reminders payload in bot_settings")
            return
        if isinstance(rows, list):
            self._pending = [
                r for r in rows
                if isinstance(r, dict) and r.get("id") and r.get("user_id")
                and r.get("due_at") and r.get("text")
            ]

    async def _save(self) -> None:
        try:
            await store.set_setting(_SETTING_KEY, json.dumps(self._pending))
        except StoreError as exc:
            LOG.warning("Could not persist reminders: %s", exc)

    # ── delivery ─────────────────────────────────────────────
    async def _fire(self, reminder: dict) -> None:
        user = self.bot.get_user(int(reminder["user_id"]))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(reminder["user_id"]))
            except discord.HTTPException as exc:
                LOG.warning("Reminder %s: cannot reach user %s (%s)",
                            reminder["id"], reminder["user_id"], exc)
                return
        try:
            await user.send(f"⏰ **Reminder:** {reminder['text']}")
        except discord.HTTPException as exc:
            LOG.warning("Reminder %s DM failed: %s", reminder["id"], exc)

    # ── commands ─────────────────────────────────────────────
    @commands.hybrid_group(name="remind", description="Timed reminders via DM.")
    async def remind(self, ctx):
        """Base: list your pending reminders, or explain usage."""
        mine = self._mine(ctx.author.id)
        if not mine:
            await ctx.send(
                "⏰ No active reminders — set one with `/remind me duration: 30m text: Drink water`.")
            return
        await self._show(ctx, mine)

    @remind.command(name="me", description="DM yourself after a duration, e.g. duration: 30m.")
    async def remind_me(self, ctx, duration: str, text: str):
        try:
            seconds = parse_span(duration)
        except ValueError as exc:
            await ctx.send(f"⚠️ {exc}")
            return
        if seconds < 30:
            await ctx.send("⚠️ Durations must be at least 30 seconds (e.g. `1m`).")
            return
        if not text.strip():
            await ctx.send("⚠️ Include what you want to be reminded about.")
            return
        if len(self._mine(ctx.author.id)) >= _MAX_PER_USER:
            await ctx.send(
                f"⚠️ You already have {_MAX_PER_USER} reminders — `/remind cancel` one first.")
            return
        reminder = {
            "id": _short_id(ctx.author.id),
            "user_id": str(ctx.author.id),
            "due_at": _iso_at(seconds),
            "text": text.strip(),
        }
        self._pending.append(reminder)
        await self._save()
        await ctx.send(
            f"⏰ Got it — I'll DM you in **{fmt_span(seconds)}**: «{reminder['text']}»")

    @remind.command(name="list", description="List your pending reminders.")
    async def remind_list(self, ctx):
        mine = self._mine(ctx.author.id)
        if not mine:
            await ctx.send("⏰ No active reminders.")
            return
        await self._show(ctx, mine)

    @remind.command(name="cancel", description="Cancel one of your reminders by id (see /remind list).")
    async def remind_cancel(self, ctx, reminder_id: str):
        mine = self._mine(ctx.author.id)
        target = next((r for r in mine if r["id"] == reminder_id.strip()), None)
        if target is None:
            await ctx.send(f"⚠️ No reminder of yours has id `{reminder_id}`.")
            return
        self._pending = [r for r in self._pending if r["id"] != target["id"]]
        await self._save()
        await ctx.send(f"✅ Reminder `{target['id']}` cancelled.")

    # ── helpers ──────────────────────────────────────────────
    def _mine(self, uid: int) -> list[dict]:
        return [r for r in self._pending if int(r["user_id"]) == uid]

    async def _show(self, ctx, mine: list[dict]) -> None:
        rows = sorted(mine, key=lambda r: r["due_at"])
        lines = [
            f"• `{r['id']}` — {r['text']} (due {r['due_at'][:16].replace('T', ' ')} UTC)"
            for r in rows
        ]
        await ctx.send("⏰ **Your reminders**\n" + "\n".join(lines))


async def setup(bot):
    await bot.add_cog(Reminders(bot))