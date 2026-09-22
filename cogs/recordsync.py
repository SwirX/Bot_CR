"""Admin record synchronisation and member data collection.

These commands are **prefix-only** (``!name``) and deliberately *not* hybrid.
They are bulk, data-mutating maintenance actions that walk every member in the
guild and rewrite hub records or mass-DM people; keeping them off the slash
surface means they can't be fired by clicking in the Discord UI, and whoever
runs them has to type the command on purpose. ``scripts/smoke_test.py`` pins
both the prefix-only set and the full command surface so neither drifts.

Four commands:

* ``!syncdb``      — mirror Discord-side identity into the hub (username,
                     display name, avatar URL, join date, primary role id).
* ``!joindatesync``— the same backfill narrowed to missing join dates only,
                     which is the one field that is routinely null because
                     ``on_member_join`` stamps ``datetime.now()`` rather than
                     Discord's real join timestamp.
* ``!askbirthday`` — DM everyone with no stored birthday a button that opens
                     the existing birthday modal.
* ``!askname``     — DM everyone with no stored real name a modal to type it,
                     then apply the cursive nickname (what ``/fixname`` does).

Planning is split into pure functions (``plan_sync`` / ``plan_collection``) so
the classification logic is unit-testable without Discord or Appwrite.
"""

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

from cogs._perms import is_bot_admin
from cogs.birthday_tracker import announce_birthday, parse_birthday
from cogs.onboarding import cursive_nickname
from data.store import StoreError, store

LOG = logging.getLogger("bot.recordsync")

# Discord rate-limits DMs hard per-guild; bursting a full roster trips 429 and
# the remainder silently fails. A short pause between DMs keeps a sweep alive.
_DM_DELAY_SECONDS = 0.6
# Writes are sequential-ish for the same reason (per-route rate limits).
_WRITE_BATCH = 10


# ── pure planning helpers ───────────────────────────────────────────────
def _avatar_url(member) -> str:
    """Best available avatar URL (guild avatar > user avatar), else ''."""
    for holder in (getattr(member, "guild_avatar", None),
                   getattr(member, "avatar", None)):
        if holder is not None:
            try:
                return str(holder.url)
            except Exception:  # noqa: BLE001 - avatar URL is best-effort
                continue
    return ""


def primary_role_id(member, default_role_id: int | None = None) -> str:
    """The member's highest role id as a string ('' when they only have @everyone).

    ``discord_users.role_id`` is a single column, so "primary" is the topmost
    non-@everyone role — the closest analogue of a "main role".
    """
    roles = [r for r in getattr(member, "roles", []) if getattr(r, "id", None)
             != default_role_id]
    if not roles:
        return ""
    top = max(roles, key=lambda r: r.position)
    return str(top.id)


def discord_payload(member, default_role_id: int | None = None) -> dict:
    """The Discord-side fields we mirror into ``discord_users`` for a member."""
    joined = getattr(member, "joined_at", None)
    if isinstance(joined, datetime):
        joined_iso = joined.astimezone(timezone.utc).isoformat()
    else:
        joined_iso = None
    return {
        "username": getattr(member, "name", "") or "",
        "display_name": getattr(member, "display_name", "") or "",
        "avatar_url": _avatar_url(member),
        "joined_at": joined_iso,
        "role_id": primary_role_id(member, default_role_id),
    }


def _is_blank(value) -> bool:
    """True for None, '', or whitespace — the shapes a null column comes back as."""
    return value is None or not str(value).strip()


def plan_sync(members, records, *, only_missing: bool = True,
              default_role_id: int | None = None) -> tuple[list, int, int]:
    """Decide what to write per member. Pure — no Discord/Appwrite I/O.

    ``members``  — objects exposing the discord.Member surface.
    ``records``  — {uid: assembled member dict} keyed by stringified user id.
    ``only_missing`` — when True (the default) skip fields that already hold a
                      value, so a sweep never clobbers curated data. ``!syncdb
                      force`` flips this to refresh everything.

    Returns ``(updates, skipped_unchanged, skipped_missing_row)`` where updates
    is a list of ``(member, payload_dict)``.
    """
    updates: list[tuple] = []
    unchanged = 0
    no_row = 0
    for member in members:
        if getattr(member, "bot", False):
            continue
        uid = str(member.id)
        record = records.get(uid)
        if record is None:
            # No hub row yet — still worth writing identity so the member exists.
            no_row += 1
            updates.append((member, discord_payload(member, default_role_id)))
            continue
        payload = discord_payload(member, default_role_id)
        if only_missing:
            payload = {k: v for k, v in payload.items()
                       if _is_blank(record.get(k)) and not _is_blank(v)}
        if not payload:
            unchanged += 1
            continue
        updates.append((member, payload))
    return updates, unchanged, no_row


def plan_collection(members, records) -> tuple[list, list]:
    """Split members into (needs_name, needs_birthday). Pure."""
    needs_name, needs_birthday = [], []
    for member in members:
        if getattr(member, "bot", False):
            continue
        record = records.get(str(member.id)) or {}
        if _is_blank(record.get("real_name")):
            needs_name.append(member)
        if _is_blank(record.get("birthday")):
            needs_birthday.append(member)
    return needs_name, needs_birthday


# ── modals / views for collection ───────────────────────────────────────
class CollectNameModal(discord.ui.Modal, title="Your real full name"):
    """Ask a member their real name, then apply the cursive nickname."""

    def __init__(self, member_id: int, store_ref=None):
        super().__init__()
        self.member_id = member_id
        self.store_ref = store_ref or store

    full_name = discord.ui.TextInput(
        label="Real full name (as on your ID card)",
        placeholder="e.g. Yasser El Joundi",
        max_length=64,
    )

    async def on_submit(self, interaction: discord.Interaction):
        name = self.full_name.value.strip()
        if not name:
            await interaction.response.send_message(
                "⚠️ Please enter your real full name.", ephemeral=True)
            return
        nickname = cursive_nickname(name)
        applied = False
        try:
            await interaction.user.edit(
                nick=nickname or None,
                reason="Record sync: real name collected via DM",
            )
            applied = True
        except discord.Forbidden:
            LOG.warning("Cannot set nickname for %s (missing permission)",
                        interaction.user.id)
        except discord.HTTPException as exc:
            LOG.warning("Failed to set nickname for %s: %s", interaction.user.id, exc)

        try:
            await self.store_ref.merge_member(interaction.user.id, {
                "real_name": name,
                "display_name": nickname or interaction.user.display_name,
            })
        except StoreError as exc:
            LOG.error("recordsync: could not persist name for %s: %s",
                      interaction.user.id, exc)
            await interaction.response.send_message(
                "⚠️ Saved locally but the club database rejected the write — "
                "an admin has been notified.", ephemeral=True)
            return

        msg = f"✅ Thanks! Your name is now **{name}**."
        if applied and nickname:
            msg += f" Your nickname is set to `{nickname}` ✨"
        elif nickname:
            msg += ("\n(⚠️ I couldn't change your nickname — I need the "
                    "*Manage Nicknames* permission.)")
        await interaction.response.send_message(msg, ephemeral=True)


class CollectNameView(discord.ui.View):
    """Persistent button that opens CollectNameModal (survives restarts)."""

    def __init__(self, member_id: int, store_ref=None):
        super().__init__(timeout=None)
        self.member_id = member_id
        self.store_ref = store_ref or store

    @discord.ui.button(label="✍️ Set my real name", style=discord.ButtonStyle.primary,
                       custom_id="recordsync:ask_name")
    async def set_name(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(
            CollectNameModal(self.member_id, self.store_ref))


class CollectBirthdayModal(discord.ui.Modal, title="Your birthday"):
    def __init__(self, store_ref=None):
        super().__init__()
        self.store_ref = store_ref or store

    date = discord.ui.TextInput(
        label="Your birthdate (YYYY-MM-DD)",
        placeholder="e.g. 2004-12-25",
        max_length=16,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            birthday_full, birthday = parse_birthday(self.date.value)
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Invalid date — try again as YYYY-MM-DD (e.g. 2004-12-25).",
                ephemeral=True)
            return
        try:
            await self.store_ref.merge_member(interaction.user.id, {
                "birthday": birthday,
                "birthday_full": birthday_full,
            })
        except StoreError as exc:
            LOG.error("recordsync: could not persist birthday for %s: %s",
                      interaction.user.id, exc)
            await interaction.response.send_message(
                "⚠️ Couldn't save that right now — try again later.",
                ephemeral=True)
            return
        await interaction.response.send_message(
            f"🎉 Saved! We'll celebrate on **{birthday_full}**.", ephemeral=True)
        # Announce only if it's actually today, and escape the name: the
        # birthday string is member-supplied and the bot posts with its own
        # mention privileges, so an unescaped name is an announcement-injection
        # vector.
        guild = interaction.guild
        if guild is not None and birthday == datetime.now().strftime("%m-%d"):
            safe_name = discord.utils.escape_mentions(
                interaction.user.display_name)[:64]
            try:
                await announce_birthday(guild, safe_name)
            except Exception:  # noqa: BLE001 - announcement is best-effort
                LOG.warning("recordsync: birthday announce failed for %s",
                            interaction.user.id)


class CollectBirthdayView(discord.ui.View):
    def __init__(self, store_ref=None):
        super().__init__(timeout=None)
        self.store_ref = store_ref or store

    @discord.ui.button(label="🎂 Set my birthday", style=discord.ButtonStyle.primary,
                       custom_id="recordsync:ask_birthday")
    async def set_birthday(self, interaction: discord.Interaction,
                           _button: discord.ui.Button):
        await interaction.response.send_modal(
            CollectBirthdayModal(self.store_ref))


# ── cog ─────────────────────────────────────────────────────────────────
class RecordSync(commands.Cog):
    """Bulk Discord→hub identity sync and member data collection."""

    def __init__(self, bot):
        self.bot = bot

    def cog_load(self):
        # Persistent views so a button in an old DM still works after a restart.
        self.bot.add_view(CollectBirthdayView(self.store))
        self.bot.add_view(CollectNameView(0, self.store))

    @property
    def store(self):
        return store

    async def _guard(self, ctx) -> bool:
        if not is_bot_admin(ctx.author):
            await ctx.send("🔒 Bot admins only.")
            return False
        if ctx.guild is None:
            await ctx.send("This only works inside the server.")
            return False
        return True

    async def _records_for(self, members) -> dict:
        try:
            return {rec["user_id"]: rec
                    for rec in await self.store.get_members([m.id for m in members])}
        except StoreError as exc:
            LOG.warning("recordsync: could not read member records: %s", exc)
            return {}

    async def _run_sync(self, ctx, *, only_missing: bool):
        if not await self._guard(ctx):
            return
        await ctx.defer()
        members = [m for m in ctx.guild.members if not m.bot]
        records = await self._records_for(members)
        updates, unchanged, no_row = plan_sync(
            members, records, only_missing=only_missing,
            default_role_id=getattr(ctx.guild, "default_role_id", None))

        written = failed = 0
        for i, (member, payload) in enumerate(updates):
            try:
                await self.store.merge_member(member.id, payload)
                written += 1
            except StoreError as exc:
                failed += 1
                LOG.warning("recordsync: write failed for %s: %s", member.id, exc)
            if (i + 1) % _WRITE_BATCH == 0:
                await asyncio.sleep(0.3)

        mode = "missing-only" if only_missing else "full overwrite"
        await ctx.send(
            f"🔄 **Sync complete** ({mode}) for **{len(members)}** member(s).\n"
            f"✅ Updated: **{written}**\n"
            f"➖ Already complete: **{unchanged}**\n"
            f"🆕 New records created: **{no_row}**\n"
            + (f"⚠️ Failed: **{failed}**" if failed else "")
        )

    @commands.command(name="syncdb")
    @commands.guild_only()
    async def syncdb(self, ctx, *, force: bool = False):
        """!syncdb — mirror username, display name, avatar, join date and role id.

        By default only fills fields that are currently blank. Pass `force`
        (e.g. `!syncdb force`) to refresh every field for everyone.
        """
        await self._run_sync(ctx, only_missing=not force)

    @commands.command(name="joindatesync")
    @commands.guild_only()
    async def joindatesync(self, ctx):
        """!joindatesync — fill in missing join dates from Discord.

        `on_member_join` records the moment the bot saw the join, which is not
        the same as Discord's real join timestamp (and is null for anyone who
        joined before that listener existed). This backfills from Discord's
        authoritative `joined_at`.
        """
        if not await self._guard(ctx):
            return
        await ctx.defer()
        members = [m for m in ctx.guild.members if not m.bot]
        records = await self._records_for(members)
        updates, unchanged, _ = plan_sync(
            members, records, only_missing=True,
            default_role_id=getattr(ctx.guild, "default_role_id", None))
        joined_only = [(m, {"joined_at": p["joined_at"]})
                       for m, p in updates if p.get("joined_at")]

        written = failed = 0
        for member, payload in joined_only:
            try:
                await self.store.merge_member(member.id, payload)
                written += 1
            except StoreError as exc:
                failed += 1
                LOG.warning("recordsync: join-date write failed for %s: %s",
                            member.id, exc)

        await ctx.send(
            f"📅 **Join-date backfill complete** for **{len(members)}** member(s).\n"
            f"✅ Filled: **{written}**\n"
            f"➖ Already had one: **{len(members) - written - failed}**\n"
            + (f"⚠️ Failed: **{failed}**" if failed else "")
        )

    async def _dm_sweep(self, ctx, targets, view_factory, label: str) -> None:
        sent = blocked = failed = 0
        for member in targets:
            try:
                await member.send(
                    f"Hey {member.display_name}! The Robotics Club needs your "
                    f"{label} — tap the button below to add it. Thanks! ✨",
                    view=view_factory(member),
                )
                sent += 1
            except discord.Forbidden:
                blocked += 1
            except discord.HTTPException as exc:
                failed += 1
                LOG.warning("recordsync: DM failed for %s: %s", member.id, exc)
            await asyncio.sleep(_DM_DELAY_SECONDS)
        return sent, blocked, failed

    @commands.command(name="askbirthday")
    @commands.guild_only()
    async def askbirthday(self, ctx):
        """!askbirthday — DM every member missing a birthday a button to set it."""
        if not await self._guard(ctx):
            return
        await ctx.defer()
        members = [m for m in ctx.guild.members if not m.bot]
        records = await self._records_for(members)
        _, needs_birthday = plan_collection(members, records)
        sent, blocked, failed = await self._dm_sweep(
            ctx, needs_birthday, lambda _m: CollectBirthdayView(self.store),
            "birthday 🎂")
        await ctx.send(
            f"🎂 **Birthday sweep done** for **{len(needs_birthday)}** member(s) "
            f"missing one.\n📨 DM'd: **{sent}**\n"
            f"🔒 DMs closed: **{blocked}**\n"
            + (f"⚠️ Failed: **{failed}**" if failed else "")
        )

    @commands.command(name="askname")
    @commands.guild_only()
    async def askname(self, ctx):
        """!askname — DM every member missing a real name a modal to type it.

        Submitting stores their real name AND sets their cursive nickname, i.e.
        exactly what `/fixname <member> -- <name>` does, but self-served.
        """
        if not await self._guard(ctx):
            return
        await ctx.defer()
        members = [m for m in ctx.guild.members if not m.bot]
        records = await self._records_for(members)
        needs_name, _ = plan_collection(members, records)
        sent, blocked, failed = await self._dm_sweep(
            ctx, needs_name, lambda m: CollectNameView(m.id, self.store),
            "real name ✍️")
        await ctx.send(
            f"✍️ **Name sweep done** for **{len(needs_name)}** member(s) "
            f"missing one.\n📨 DM'd: **{sent}**\n"
            f"🔒 DMs closed: **{blocked}**\n"
            + (f"⚠️ Failed: **{failed}**" if failed else "")
        )


async def setup(bot):
    await bot.add_cog(RecordSync(bot))