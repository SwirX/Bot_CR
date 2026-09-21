"""mc-link — Discord side of the Discord ↔ Minecraft single sign-on layer.

The robotics_hub TablesDB backend is the source of truth; this bot is its
authenticated client and the ONLY component that mints links and credentials.
A Paper plugin (deployed alongside the server) consumes backend state — it
validates the one-time code against ``minecraft_otp.otp_hash``
(sha256(otp_salt + code)), disables the OTP and activates the pre-seeded
``discord_mc_links`` row; this bot's watcher picks the activation up.

Trust invariant (hard rule, from PLAN.md §0): **Minecraft is an untrusted
client boundary.** Never derive an auth decision from anything a Minecraft
client claims (username, UUID, permissions, "logged in" state). The only real
proofs are (1) Discord identity and (2) knowledge of a secret this bot issued.

Regressions vs the legacy backend (reported): /mcpass and the Devices/IP
drill-downs are gone (the hub has no credential/device store), and the
new-IP / password-change challenge watchers are gone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

import config
from cogs._mc_crypto import dt_friendly, iso_now, new_link_code, parse_iso
from cogs.minecraft import Minecraft, _USERNAME_RE
from data.store import StoreError, store
from i18n.core import resolve_member_lang, t

LOG = logging.getLogger("bot.mclink")


# ── Cog ────────────────────────────────────────────────────────────────
class McLink(commands.Cog):
    """One-time link codes (OTP) and the activation watcher."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._stop = asyncio.Event()
        self._watch_task: asyncio.Task | None = None

    # ── lifecycle ─────────────────────────────────────────────
    def _configured(self) -> bool:
        return bool(config.MC_LINK_SECRET)

    async def cog_load(self) -> None:
        """Start the link-activation watcher (mc-link only when configured)."""
        if self._configured():
            self._watch_task = asyncio.create_task(self._watch())

    def cog_unload(self) -> None:
        self._stop.set()
        if self._watch_task is not None:
            self._watch_task.cancel()

    async def _lang(self, ctx) -> str:
        locale = str(ctx.interaction.locale) if ctx.interaction else None
        return await resolve_member_lang(ctx.author.id, locale)

    async def _dm(self, user_id: int, content: str,
                  view: discord.ui.View | None = None) -> bool:
        """DM a member; False when DMs are closed / the user can't be found."""
        try:
            user = await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return False
        try:
            if view is not None:
                await user.send(content, view=view)
            else:
                await user.send(content)
            return True
        except discord.HTTPException:
            return False

    def _mc(self) -> Minecraft | None:
        return self.bot.get_cog("Minecraft")  # type: ignore[return-value]

    def _member_in_guilds(self, discord_id: int) -> discord.Member | None:
        for guild in self.bot.guilds:
            member = guild.get_member(discord_id)
            if member is not None:
                return member
        return None

    async def _sync_display_link(self, discord_id: int, username: str) -> None:
        """Keep the member's links.minecraft identity readable by the hub."""
        record = (await store.get_member(discord_id)) or {}
        links = Minecraft._links_of(record)
        links["minecraft"] = {
            "username": username,
            "type": "linked",
            "uuids": [],
            "linked_at": dt_friendly(),
        }
        await store.merge_member(discord_id, {"links": json.dumps(links),
                                              "mc_username": username})

    # ── /mclink ───────────────────────────────────────────────
    @commands.hybrid_command(
        name="mclink",
        description="Link your Minecraft username to Discord via a one-time code.")
    @commands.guild_only()
    @commands.cooldown(2, 60, commands.BucketType.user)
    async def mclink(self, ctx: commands.Context, username: str):
        """Start the link handshake: this bot DMs a code typeable in-game.

        The code is single-use, expires in ``MC_LINK_CODE_TTL`` seconds, and is
        never shown in the guild channel. The in-game claim is made by the
        Paper plugin against the backend — this command only mints the OTP
        (``minecraft_otp`` row + pre-seeded inactive ``discord_mc_links`` row)
        and the watcher completes the link once the plugin activates it.
        """
        lang = await self._lang(ctx)
        if not self._configured():
            await ctx.send(t("mc.unconfigured", lang))
            return
        name = (username or "").strip()
        if not _USERNAME_RE.match(name):
            await ctx.send(t("mclink.bad_name", lang, name=name))
            return
        profile = await store.mc_get_auth(name)
        if profile and profile.get("status") == "linked":
            owner = str(profile.get("discord_id") or "")
            if owner and owner != str(ctx.author.id):
                await ctx.send(t("mclink.taken", lang, name=name))
                return
        code = new_link_code()
        salt = secrets.token_urlsafe(24)
        otp_hash = hashlib.sha256(f"{salt}{code}".encode("utf-8")).hexdigest()
        expiry = (datetime.now(timezone.utc)
                  + timedelta(seconds=config.MC_LINK_CODE_TTL)).isoformat()
        try:
            otp_id = await store.mc_create_otp(
                name, ctx.author.id, expires_at=expiry,
                username_hint=ctx.author.display_name or ctx.author.name,
                otp_salt=salt, otp_hash=otp_hash)
        except StoreError as exc:
            LOG.error("mclink: could not mint OTP for %s: %s", ctx.author.id, exc)
            await ctx.send(t("mclink.store_fail", lang))
            return
        minutes = max(1, config.MC_LINK_CODE_TTL // 60)
        dm_text = (f"{t('mclink.announce', lang)}\n\n"
                   + t("mclink.dm_code", lang, code=code, name=name,
                       minutes=minutes))
        if not await self._dm(ctx.author.id, dm_text):
            # DMs closed → the code is useless; disable the OTP so nothing
            # lingers. The inactive link row stays harmlessly behind.
            try:
                await store.mc_expire_otp(otp_id)
            except StoreError:
                pass
            await ctx.send(t("mclink.dm_closed", lang))
            return
        await ctx.send(t("mclink.sent", lang, minutes=minutes))

    # ── watcher ───────────────────────────────────────────────
    async def _watch(self) -> None:
        """5 s poll loop: OTP-activation follow-up on link rows."""
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            # Only reachable in test harnesses that never connect the client;
            # production always has a live gateway before the watcher spins.
            LOG.warning("mc-link watcher idle: client never started")
            return
        while not self._stop.is_set():
            try:
                await self._watch_links()
            except Exception as exc:  # noqa: BLE001 - a bad cycle must never die
                LOG.warning("mc-link watcher cycle failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=config.MC_LINK_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _watch_links(self) -> None:
        """Thanks DM, role, display link + audit once the plugin activates a
        pre-seeded ``discord_mc_links`` row (the OTP claim).

        Markers make delivery at-least-once across restarts: a link row is
        only processed once its ``$updatedAt`` passes the persisted marker
        (``mc_link.last_claim``, initialized to now on first boot so old
        activations are skipped), and the marker only advances for rows whose
        DM fully succeeded.
        """
        marker = await store.get_setting("mc_link.last_claim") or iso_now()
        marker_dt = parse_iso(marker)
        try:
            links = await store.mc_list_active_links()
        except StoreError:
            return
        latest_raw = marker
        latest_dt = marker_dt
        for link in links:
            updated_raw = str(link.get("$updatedAt") or "")
            updated_dt = parse_iso(updated_raw)
            if updated_dt is None or marker_dt is None \
                    or updated_dt <= marker_dt:
                continue
            if await self._claim_link(link) and (
                    latest_dt is None or updated_dt > latest_dt):
                latest_raw, latest_dt = updated_raw, updated_dt
        if latest_raw != marker:
            await store.set_setting("mc_link.last_claim", latest_raw)

    async def _claim_link(self, link: dict) -> bool:
        username = link.get("pair_key") or ""
        discord_id = int(link.get("discord_user") or 0)
        if not username or not discord_id:
            return True  # malformed — don't retry forever
        lang = await resolve_member_lang(discord_id)
        if not await self._dm(discord_id,
                              t("mclink.dm_thanks", lang, name=username)):
            return False  # DM failed — keep retrying next cycle (at-least-once)
        mc = self._mc()
        member = self._member_in_guilds(discord_id)
        if member is not None and mc is not None:
            try:
                await mc._ensure_mc_role(member)
            except Exception as exc:  # noqa: BLE001 - tagging is best-effort
                LOG.warning("mc-link role assign failed for %s: %s",
                            discord_id, exc)
        try:
            await self._sync_display_link(discord_id, username)
            await store.log_moderation(
                action="mc_link", target_id=discord_id, target_name=username,
                moderator_id=discord_id,
                reason="mc-link OTP claimed")
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            LOG.warning("mc-link display/audit failed for %s: %s",
                        discord_id, exc)
        return True  # the DM is at-least-once; role/link/audit are idempotent


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(McLink(bot))