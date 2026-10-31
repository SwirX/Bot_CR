"""Pays Discord (hub) XP for Minecraft gameplay.

The game is the thing that should be worth playing, so active play in
RoboticsCMC mints hub XP at ``config.MC_XP_RATIO`` per point of gameplay XP.
Gameplay XP is awarded by the server at 1 point per 30s of *active* play and
stops at its own daily-active cap, so it is already a proxy for real
engagement — it cannot be farmed by idling in a renderer's tab.

Three rules keep the faucet honest:

* **Deltas, never totals.** Only the rise in ``gameplay_xp`` since this bot
  last paid for it is converted, tracked by the ``mc_xp_credited`` watermark.
  Reading the absolute total would re-grant a member's whole history on every
  run, and the watermark survives redeploys because it lives in Appwrite.
* **First sight pays nothing.** A profile with no watermark is stamped with
  its current ``gameplay_xp`` and credited zero, so enabling this mid-season
  does not hand everyone a windfall for play that already happened.
* **A daily cap, reset by date.** ``MC_XP_DAILY_CAP`` bounds hub XP per member
  per day independently of the server's active-play cap, so the two knobs
  cannot compound. XP earned past the cap is *dropped* rather than banked:
  the watermark still advances, otherwise tomorrow's reset would release a
  backlog that was never earned against today's cap.

Runs on its own timer rather than piggybacking the 30-minute profile sync: a
sync outage must not stall payouts, and an extra sync must not double-pay.
"""

import logging
from datetime import datetime, timezone

from discord.ext import commands, tasks

import config
from data.store import StoreError, store

LOG = logging.getLogger("bot.mc_xp_sync")


class McXpSync(commands.Cog):
    """Minecraft gameplay XP -> Discord hub XP conversion."""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        await self.bot.wait_until_ready()
        if config.MC_XP_SYNC_ENABLED and not self.sweep.is_running():
            self.sweep.start()
            LOG.info(
                "Minecraft->hub XP conversion active: %dx, %d hub XP/member/day",
                config.MC_XP_RATIO, config.MC_XP_DAILY_CAP,
            )

    async def cog_unload(self):
        self.sweep.cancel()

    @tasks.loop(seconds=max(60, config.MC_XP_SYNC_SECONDS), name="mc-xp-sync")
    async def sweep(self):
        try:
            granted, capped, seeded = await self.sweep_once()
        except StoreError as exc:
            LOG.error("gameplay->hub XP sweep failed: %s", exc)
            return
        if granted or capped or seeded:
            LOG.info(
                "gameplay->hub XP: +%d hub XP across members "
                "(%d cap-limited, %d watermark-seeded)",
                granted, capped, seeded,
            )

    async def sweep_once(self) -> tuple[int, int, int]:
        """One pass. Returns (hub XP granted, capped members, newly seeded)."""
        profiles = await store.list_mc_profiles()
        today = datetime.now(timezone.utc).date().isoformat()

        granted = capped = seeded = 0
        payouts: dict[str, dict] = {}
        marks: dict[str, dict] = {}

        for profile in profiles:
            # ProfileSyncService creates the row with rowId = discordUserId, so
            # the document id is the Discord id. The ``discord_user``
            # relationship column is not used here: Appwrite returns
            # relationships as a list whose shape varies with API version.
            member_id = str(profile.get("$id") or "").strip()
            if not member_id:
                continue

            gameplay = int(profile.get("gameplay_xp") or 0)
            watermark = int(profile.get("mc_xp_credited") or 0)
            day = str(profile.get("mc_xp_credited_day") or "")
            today_total = int(profile.get("mc_xp_credited_today") or 0)

            if day != today:
                day, today_total = today, 0

            if watermark == 0 and gameplay > 0:
                # Pre-existing history: adopt it as already-paid so this
                # feature cannot retro-grant. Zero XP profiles keep their 0
                # watermark and start paying from their first real point.
                marks[member_id] = {
                    "credited": gameplay, "day": day, "credited_today": 0,
                }
                seeded += 1
                continue

            delta = gameplay - watermark
            if delta <= 0:
                if day != str(profile.get("mc_xp_credited_day") or ""):
                    marks[member_id] = {
                        "credited": watermark, "day": day, "credited_today": 0,
                    }
                continue

            award = delta * config.MC_XP_RATIO
            allowed = config.MC_XP_DAILY_CAP - today_total
            limited = award > allowed
            award = max(0, min(award, allowed))

            # The watermark advances past XP the cap refused to pay. Banking it
            # would release a backlog when the cap resets tomorrow.
            marks[member_id] = {
                "credited": gameplay,
                "day": day,
                "credited_today": today_total + award,
            }
            if award:
                payouts[member_id] = {
                    "xp": award,
                    "_bootstrap": {"username": profile.get("username") or ""},
                }
                granted += award
            if limited:
                capped += 1

        if payouts:
            # Additive delta; the store re-derives discord_data.level from the
            # resulting total, so the hub rank stays consistent.
            await store.flush_member_activity(payouts)
        for member_id, mark in marks.items():
            await store.record_mc_xp_credit(member_id, **mark)
        return granted, capped, seeded


async def setup(bot):
    await bot.add_cog(McXpSync(bot))
