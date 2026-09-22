import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

import config
from data.store import store
from data.store import StoreError
from cogs.birthday_tracker import announce_birthday, parse_birthday
from cogs._perms import mod_perms
from data.names import decursive, has_cursive

LOG = logging.getLogger("bot.onboarding")

# ── Unicode Mathematical Bold Script (the common "copypaste cursive") ──
# 𝓐 == U+1D4D0 .. 𝓩 == U+1D4E9  (capitals)
# 𝓪 == U+1D4EA .. 𝓩 small == U+1D503  (lowercase)
_CAP_OFFSET = 0x1D4D0 - ord("A")
_LOW_OFFSET = 0x1D4EA - ord("a")

NICKNAME_MAX = 32  # Discord's hard limit


def to_cursive(text: str) -> str:
    """Convert Latin letters to Unicode Mathematical Bold Script.

    Non-Latin characters (e.g. Arabic names), digits and spaces pass through
    unchanged — a name like "حمدي أحمد" keeps its native script.
    """
    out = []
    for ch in text:
        code = ord(ch)
        if "A" <= ch <= "Z":
            out.append(chr(code + _CAP_OFFSET))
        elif "a" <= ch <= "z":
            out.append(chr(code + _LOW_OFFSET))
        else:
            out.append(ch)
    return "".join(out)


def cursive_nickname(real_name: str, *, max_len: int = NICKNAME_MAX) -> str:
    """Build a title-cased, cursive nickname from a real full name."""
    words = real_name.strip().split()
    if not words:
        return ""
    title = " ".join(word.capitalize() for word in words)
    cursive = to_cursive(title)
    if len(cursive) <= max_len:
        return cursive
    # Trim on a word boundary first, else a hard cut.
    cut = cursive[:max_len]
    boundary = cut.rfind(" ")
    if boundary > 0:
        cut = cut[:boundary]
    return cut.strip()


def find_text_channel(guild: discord.Guild, name: str):
    return discord.utils.get(guild.text_channels, name=name)


def plan_namesweep(members, records):
    """Classify members for /namesweep. Pure — no Discord/store I/O.

    ``members``  — objects with ``.id`` and ``.nick`` (discord.Member works).
    ``records``  — {uid: assembled member dict} from ``store.get_members``.

    Returns (backfill, skipped, name_dm, birthday_dm):
      backfill     — [(member, plain_name)] cursive nick, no stored name yet
      skipped      — [member] has a stored real name already
      name_dm      — [member] no stored real name and nothing to backfill
      birthday_dm  — [member] no stored birthday
    """
    backfill, skipped, name_dm, birthday_dm = [], [], [], []
    for member in members:
        record = records.get(str(member.id)) or {}
        has_name = bool(record.get("real_name"))
        nick = member.nick or ""
        if has_cursive(nick):
            if has_name:
                skipped.append(member)
            else:
                plain = decursive(nick).strip()
                if plain:
                    backfill.append((member, plain))
                else:
                    name_dm.append(member)
        elif not has_name:
            name_dm.append(member)
        if not record.get("birthday"):
            birthday_dm.append(member)
    return backfill, skipped, name_dm, birthday_dm


# ── Name modal ──────────────────────────────────────────────────────────
class NameModal(discord.ui.Modal, title="Your real full name"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    full_name = discord.ui.TextInput(
        label="Real full name (as in your ID card)",
        placeholder="e.g. Yasser El Joundi",
        max_length=64,
    )

    async def on_submit(self, interaction: discord.Interaction):
        member = interaction.user
        name = self.full_name.value.strip()
        if not name:
            await interaction.response.send_message("⚠️ Please enter your real full name.", ephemeral=True)
            return

        nickname = cursive_nickname(name)
        nickname_applied = False
        if nickname:
            try:
                await member.edit(nick=nickname, reason="Onboarding: real name → cursive nickname")
                nickname_applied = True
            except discord.Forbidden:
                LOG.warning("Cannot change nickname of %s (permission missing)", member)
            except discord.HTTPException as exc:
                LOG.warning("Failed to set nickname for %s: %s", member, exc)

        # Grant verified roles, drop the temp role.
        guild = interaction.guild
        temp_role = discord.utils.get(guild.roles, name=config.ROLE_TEMP)
        verified_role = discord.utils.get(guild.roles, name=config.ROLE_VERIFIED)
        member_role = discord.utils.get(guild.roles, name=config.ROLE_MEMBER)
        try:
            role_updates = []
            if verified_role:
                role_updates.append(("add", verified_role))
            if member_role:
                role_updates.append(("add", member_role))
            if temp_role and temp_role in member.roles:
                role_updates.append(("remove", temp_role))
            for action, role in role_updates:
                if action == "add":
                    await member.add_roles(role, reason="Onboarding verified")
                else:
                    await member.remove_roles(role, reason="Verification complete")
        except discord.Forbidden:
            LOG.warning("Missing permissions to update roles for %s", member)

        # Persist the verified profile.
        try:
            await store.merge_member(member.id, {
                "real_name": name,
                "display_name": nickname or member.display_name,
                "verified": True,
            })
        except StoreError:
            LOG.error("Failed to persist onboarding profile for %s", member)

        # Best-effort join: match this Discord user to their club-registry row.
        try:
            await store.maybe_auto_link_club_member(member.id, name)
        except StoreError:
            LOG.warning("onboarding: auto-link club member failed for %s",
                        member.id)

        ok_name = nickname or name
        reply = f"✅ Verified! Your nickname is now **{ok_name}**."
        if not nickname_applied:
            reply += "\n(⚠️ I couldn't change your nickname — I need the *Manage Nicknames* permission.)"
        await interaction.response.send_message(reply, ephemeral=True)

        await self.cog.prompt_birthday(member)


# ── Persistent views ────────────────────────────────────────────────────
class StartOnboardingView(discord.ui.View):
    """Shows the 'verify with your real name' button (persists across restarts)."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="✅ Start onboarding", style=discord.ButtonStyle.success,
                       custom_id="onboarding_start")
    async def start(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(NameModal(self.cog))


class BirthdayPromptView(discord.ui.View):
    """Opens the birthday modal (persists across restarts)."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="🎂 Set your birthday", style=discord.ButtonStyle.primary,
                       custom_id="birthday_set")
    async def set_birthday(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(BirthdayModal(self.cog))


class BirthdayModal(discord.ui.Modal, title="Enter Your Birthday"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    date = discord.ui.TextInput(label="Your birthdate", placeholder="e.g. 2004-12-25", max_length=16)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            birthday_full, birthday = parse_birthday(self.date.value)
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Invalid date! Try again (e.g. 2004-12-25).", ephemeral=True
            )
            return

        try:
            await store.merge_member(interaction.user.id, {
                "birthday": birthday,
                "birthday_full": birthday_full,
            })
            await interaction.response.send_message(
                f"🎉 Your birthday has been saved: {birthday_full}", ephemeral=True
            )
            await self.cog.announce_birthday_if_today(interaction.guild, interaction.user)
        except StoreError:
            LOG.error("Failed to persist birthday for %s", interaction.user)
            await interaction.response.send_message(
                "⚠️ Could not save your birthday right now — try again later.", ephemeral=True
            )


class Onboarding(commands.Cog):
    """Join → temp role → real-name modal → cursive nickname → roles → birthday.

    Replaces the old reaction-based verification (which let anyone verify or
    kick anyone else by reacting to a verification message).
    """

    def __init__(self, bot):
        self.bot = bot
        # Re-register persistent views so buttons in old DMs keep working
        # after a restart.
        self.bot.add_view(StartOnboardingView(self))
        self.bot.add_view(BirthdayPromptView(self))

    # ── join flow ─────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member):
        if member.bot:
            return

        # 1. Assign the temp role.
        temp_role = discord.utils.get(member.guild.roles, name=config.ROLE_TEMP)
        if temp_role:
            try:
                await member.add_roles(temp_role, reason="Onboarding: pending verification")
            except discord.Forbidden:
                LOG.warning("Cannot assign %r to %s (permission missing)", config.ROLE_TEMP, member)

        # 2. Seed the member record so stats/XP have a doc to attach to.
        try:
            await store.merge_member(member.id, {
                "username": member.name,
                "joined_at": datetime.now(timezone.utc).isoformat(),
            })
        except StoreError:
            LOG.error("Failed to seed member record for %s", member)

        # 3. DM the onboarding button.
        try:
            await member.send(
                "👋 Welcome to the Robotics Club server!\n\n"
                "To finish onboarding, tap the button and enter your **real full name** "
                "— you'll get a fancy cursive nickname. 🎨",
                view=StartOnboardingView(self),
            )
        except discord.Forbidden:
            channel = find_text_channel(member.guild, config.CHANNEL_BOTLOG)
            if channel:
                await channel.send(
                    f"⚠️ {member.mention} has DMs closed — they can't start onboarding from here. "
                    "Ask them to open DMs for this server and re-join, or message an admin."
                )

    async def prompt_birthday(self, member: discord.Member):
        """Ask for the birthday after verification (skip if already known)."""
        try:
            record = await store.get_member(member.id)
        except StoreError:
            record = None
        if record and record.get("birthday"):
            return
        try:
            await member.send(
                "One last step! 🎂 Tell me your birthday so we can celebrate it every year.",
                view=BirthdayPromptView(self),
            )
        except discord.Forbidden:
            LOG.warning("Cannot DM birthday prompt to %s", member)

    async def announce_birthday_if_today(self, guild: discord.Guild, member: discord.Member):
        """Announce in the announcements channel if today is the member's birthday."""
        try:
            record = await store.get_member(member.id)
        except StoreError:
            record = None
        birthday = (record or {}).get("birthday")
        if not birthday:
            return
        today = datetime.now().strftime("%m-%d")
        if birthday == today:
            name = record.get("display_name") or record.get("real_name") or member.display_name
            await announce_birthday(guild, name)

    # ── staff admin ────────────────────────────────────────────
    @commands.hybrid_group(name="admin",
                           description="(staff) Administrative tools — namesweep and friends.")
    @commands.guild_only()
    async def admin(self, ctx):
        """Staff admin tools — use a subcommand (e.g. /admin namesweep)."""
        await ctx.send("🛠️ Admin tools — try `/admin namesweep` to backfill real "
                       "names from cursive nicknames and nudge members with "
                       "missing name/birthday.")

    @admin.command(name="namesweep",
                   description="Backfill real names from cursive nicknames and DM members missing name/birthday.")
    @mod_perms(manage_nicknames=True)
    async def namesweep(self, ctx):
        """Scan server nicknames: save plain names for cursive ones, then DM
        members who still have no stored real name (and separately, no
        birthday) with a tagged reminder to use /profile setname and
        /profile setbirthday. Members whose names are already stored are
        skipped."""
        members = [m for m in ctx.guild.members if not m.bot]
        try:
            records = {rec["user_id"]: rec
                       for rec in await store.get_members([m.id for m in members])}
        except StoreError as exc:
            LOG.warning("namesweep: could not read member records: %s", exc)
            await ctx.send("⚠️ Couldn't read member records right now — try again later.")
            return
        backfill, skipped, name_dm, birthday_dm = plan_namesweep(members, records)

        backfilled = 0
        for member, plain in backfill:
            try:
                await store.merge_member(member.id, {"real_name": plain})
                backfilled += 1
            except StoreError as exc:
                LOG.warning("namesweep: could not persist name for %s: %s", member.id, exc)
            try:
                await member.edit(nick=plain, reason="namesweep: removed cursive from nickname")
            except (discord.Forbidden, discord.HTTPException):
                LOG.info("namesweep: could not de-cursive nickname of %s", member.id)

        name_sent = name_blocked = 0
        for member in name_dm:
            try:
                await member.send(
                    f"Hey {member.mention} — we don't have your real name on file yet! "
                    "Set it with **/profile setname** to get your cursive nickname. ✨"
                )
                name_sent += 1
            except discord.Forbidden:
                name_blocked += 1

        bday_sent = bday_blocked = 0
        for member in birthday_dm:
            try:
                await member.send(
                    f"Hey {member.mention}! We're missing your birthday 🎂 — "
                    "set it with **/profile setbirthday** so we can celebrate you."
                )
                bday_sent += 1
            except discord.Forbidden:
                bday_blocked += 1

        await ctx.send(
            f"🧹 **Namesweep complete** — backfilled **{backfilled}** real name(s) "
            f"from cursive nicknames ({len(skipped)} already stored, skipped).\n"
            f"📨 Name nudges: {name_sent} DM'd, {name_blocked} blocked (DMs closed).\n"
            f"🎂 Birthday nudges: {bday_sent} DM'd, {bday_blocked} blocked (DMs closed)."
        )


async def setup(bot):
    await bot.add_cog(Onboarding(bot))