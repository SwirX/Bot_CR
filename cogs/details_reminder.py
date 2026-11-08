"""Private reminders to complete onboarding details."""

import asyncio
import logging

import discord
from discord.ext import commands

import config
from cogs._perms import is_bot_admin
from cogs.onboarding import StartOnboardingView
from data.names import has_cursive

LOG = logging.getLogger("bot.details_reminder")
LATIN_MESSAGE = (
    "Salam! Xfnak bli ba9i ma3titinax smiya dyalk kamla, clicki 3la botona li l7t "
    "bax tdkhlha, ghadi n3tiwk wa7d smiya cursive o mnha n9dro ndiro lik verification "
    "fl server, t9dr tgul 7ta wax dri ula bnt o 3idmilad dyalk bax nsifto msg fl server "
    "mnin ywsl bax nbarko lik"
)
ARABIC_MESSAGE = (
    "سلام! شفناك بلي باقي ما عطيتيناش سميتك كاملة، كليكي على البوطونة اللي لتحت "
    "باش تدخلها، غادي نعطيوك واحد السمية كيرسيف ومنها نقدرو نديرو ليك ڤيريفيكاسيون "
    "فالسيرفر، تقدر تقول حتى واش دري ولا بنت وعيد ميلاد ديالك باش نصيفطو مساج فالسيرفر "
    "منين يوصل باش نباركو ليك"
)
MESSAGE = f"{LATIN_MESSAGE}\n\n\n{ARABIC_MESSAGE}"


def needs_reminder(member, pending_id: int, new_member_id: int) -> bool:
    roles = {role.id for role in member.roles}
    return (not member.bot and pending_id in roles and new_member_id not in roles
            and not has_cursive(member.nick))


def reminder_view(onboarding):
    view = StartOnboardingView(onboarding)
    view.start.label = "Dkhel ma3loumatk"
    return view


class DetailsReminder(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="detailsreminder")
    @commands.guild_only()
    @commands.max_concurrency(1, per=commands.BucketType.guild, wait=False)
    async def remind_details(self, ctx):
        """DM eligible pending members a reminder to complete their details."""
        if not is_bot_admin(ctx.author):
            await ctx.send("Bot admins only.")
            return
        pending = discord.utils.get(ctx.guild.roles, name=config.ROLE_TEMP)
        new_member = ctx.guild.get_role(config.ROLE_NEW_MEMBER_ID)
        onboarding = self.bot.get_cog("Onboarding")
        if pending is None or new_member is None or onboarding is None:
            await ctx.send("The onboarding roles or form are unavailable; no reminders sent.")
            return
        try:
            members = [member async for member in ctx.guild.fetch_members(limit=None)]
        except discord.HTTPException as exc:
            LOG.warning("Could not fetch members for details reminders: %s", exc)
            await ctx.send("Couldn't fetch the server members; no reminders sent.")
            return
        targets = [member for member in members
                   if needs_reminder(member, pending.id, new_member.id)]
        if not targets:
            await ctx.send("No pending members need a reminder.")
            return
        await ctx.send(f"Sending details reminders to up to {len(targets)} pending member(s).")
        sent = blocked = skipped = failed = 0
        for member in targets:
            try:
                current = await ctx.guild.fetch_member(member.id)
            except discord.NotFound:
                skipped += 1
                continue
            except discord.HTTPException as exc:
                failed += 1
                LOG.warning("Could not recheck details reminder for %s: %s", member.id, exc)
                continue
            if not needs_reminder(current, pending.id, new_member.id):
                skipped += 1
                continue
            try:
                await current.send(MESSAGE, view=reminder_view(onboarding))
                sent += 1
            except discord.Forbidden:
                blocked += 1
            except discord.HTTPException as exc:
                failed += 1
                LOG.warning("Details reminder failed for %s: %s", member.id, exc)
            await asyncio.sleep(0.6)
        await ctx.send(f"Details reminders: {sent} sent, {blocked} DMs closed, "
                       f"{skipped} no longer eligible, {failed} failed.")


async def setup(bot):
    await bot.add_cog(DetailsReminder(bot))
