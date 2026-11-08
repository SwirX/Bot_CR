import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

import config
from cogs._gender import normalize_gender
from cogs._member_identity import member_for_interaction
from cogs._message_moderation import delete_message
from cogs._perms import mod_perms
from cogs.birthday_tracker import announce_birthday, parse_birthday
from data.names import decursive, has_cursive
from data.store import StoreError, store

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
    gender = discord.ui.TextInput(
        label="Gender / Genre",
        placeholder="Male / Female or Homme / Femme",
        max_length=32,
    )
    birthday = discord.ui.TextInput(
        label="Birthday (optional, YYYY-MM-DD)",
        placeholder="e.g. 2004-12-25",
        max_length=10,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        name = self.full_name.value.strip()
        if not name:
            await interaction.response.send_message(
                "⚠️ Please enter your real full name.", ephemeral=True)
            return
        gender = normalize_gender(self.gender.value)
        if gender is None:
            await interaction.response.send_message(
                "⚠️ Please reopen the form and enter Male/Female or Homme/Femme.",
                ephemeral=True)
            return
        profile = {"real_name": name, "gender": gender}
        if self.birthday.value.strip():
            try:
                full_date, month_day = parse_birthday(self.birthday.value)
            except ValueError:
                await interaction.response.send_message(
                    "⚠️ Please open the form again and enter a valid birthday as YYYY-MM-DD.",
                    ephemeral=True)
                return
            profile.update({"birthday_full": full_date, "birthday": month_day})
        await interaction.response.defer(ephemeral=True, thinking=True)
        member = await member_for_interaction(interaction)
        if member is None:
            await interaction.followup.send(
                "⚠️ I couldn't find you in the club server. Please contact an admin.",
                ephemeral=True)
            return

        try:
            await store.merge_member(member.id, profile)
        except StoreError as exc:
            LOG.error("Failed to save onboarding profile for %s: %s", member.id, exc)
            await interaction.followup.send(
                "⚠️ I couldn't save your name. Please try again later or contact an admin.",
                ephemeral=True)
            return

        nickname = cursive_nickname(name)
        try:
            await member.edit(nick=nickname, reason="Onboarding: real name → cursive nickname")
        except discord.HTTPException as exc:
            LOG.warning("Failed to set nickname for %s: %s", member.id, exc)
            await interaction.followup.send(
                "⚠️ Your name was saved, but I couldn't change your server nickname. "
                "Please contact an admin to finish verification.", ephemeral=True)
            return

        # Grant verified roles, drop the temp role.
        guild = member.guild
        temp_role = discord.utils.get(guild.roles, name=config.ROLE_TEMP)
        verified_role = discord.utils.get(guild.roles, name=config.ROLE_VERIFIED)
        member_role = discord.utils.get(guild.roles, name=config.ROLE_MEMBER)
        new_member_role = guild.get_role(config.ROLE_NEW_MEMBER_ID)
        gender_id = config.ROLE_MALE_ID if gender == "male" else config.ROLE_FEMALE_ID
        other_gender_id = config.ROLE_FEMALE_ID if gender == "male" else config.ROLE_MALE_ID
        gender_role = guild.get_role(gender_id)
        other_gender_role = guild.get_role(other_gender_id)
        if not all((verified_role, new_member_role, gender_role)):
            LOG.error("An onboarding role is missing in guild %s", guild.id)
            await interaction.followup.send(
                "⚠️ Your name was saved, but verification is unavailable. Please contact an admin.",
                ephemeral=True)
            return

        try:
            roles = [role for role in (new_member_role, gender_role, member_role, verified_role)
                     if role is not None and role not in member.roles]
            if roles:
                await member.add_roles(*roles, reason="Onboarding verified")
            if other_gender_role and other_gender_role in member.roles:
                await member.remove_roles(other_gender_role, reason="Onboarding gender selected")
            if temp_role and temp_role in member.roles:
                await member.remove_roles(temp_role, reason="Verification complete")
        except discord.HTTPException as exc:
            LOG.warning("Failed to update roles for %s: %s", member, exc)
            await interaction.followup.send(
                "⚠️ Your name was saved, but I couldn't finish assigning your roles. "
                "Please contact an admin.", ephemeral=True)
            return

        await interaction.followup.send(
            f"✅ Verified! Your nickname is now **{nickname}**.", ephemeral=True)

        try:
            await asyncio.wait_for(
                store.merge_member(member.id, {"display_name": nickname, "verified": True}),
                timeout=10)
            await asyncio.wait_for(store.maybe_auto_link_club_member(member.id, name), timeout=10)
        except (StoreError, TimeoutError) as exc:
            LOG.warning("onboarding: profile follow-up failed for %s: %s", member.id, exc)


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

        # 1. Assign the pending role.
        temp_role = discord.utils.get(member.guild.roles, name=config.ROLE_TEMP)
        if temp_role is None:
            LOG.warning("Pending onboarding role is missing in guild %s", member.guild.id)
        else:
            try:
                await member.add_roles(temp_role, reason="Onboarding: pending verification")
            except discord.HTTPException as exc:
                LOG.warning("Cannot assign pending role to %s: %s", member.id, exc)

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
                "To finish onboarding, tap the button and enter your **real full name** and gender. "
                "You can also add your birthday for birthday messages. 🎨",
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

    @admin.command(name="deletemessage", description="Delete one message by ID in a channel.")
    async def admin_delete_message(self, ctx, message_id: str,
                                   channel: discord.TextChannel | None = None):
        """Delete one message from this channel or another server text channel."""
        await delete_message(ctx, message_id, channel)

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
