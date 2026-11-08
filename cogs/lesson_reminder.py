"""Private onboarding reminders for the C language lesson."""

import asyncio
import logging

import discord
from discord.ext import commands

import config
from cogs._perms import is_bot_admin
from cogs.onboarding import StartOnboardingView
from data.names import has_cursive
from data.store import StoreError, store

LOG = logging.getLogger("bot.lesson_reminder")
MESSAGES = {
    "fr": (
        "Bonjour ! Pour participer au cours de langage C d’aujourd’hui, complète ta "
        "vérification : utilise le bouton ci-dessous pour renseigner ton nom complet "
        "et ton genre. La date de naissance est facultative.",
        "Compléter mes informations",
    ),
    "en": (
        "Hello! To take part in today's C language lesson, please finish verification: "
        "use the button below to enter your full name and gender. Your birthday is optional.",
        "Complete my details",
    ),
    "ar": (
        "مرحباً! للمشاركة في درس لغة C اليوم، يرجى إكمال التحقق: اضغط على الزر أدناه "
        "وأدخل اسمك الكامل وجنسك. تاريخ الميلاد اختياري.",
        "إكمال معلوماتي",
    ),
}


def needs_reminder(member, pending_id: int, new_member_id: int) -> bool:
    roles = {role.id for role in member.roles}
    return (not member.bot and pending_id in roles and new_member_id not in roles
            and not has_cursive(member.nick))


def reminder_text(lang: str | None) -> tuple[str, str]:
    return MESSAGES.get(str(lang or "").strip().lower(), MESSAGES["fr"])


class LessonReminder(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="rappelc")
    @commands.guild_only()
    @commands.max_concurrency(1, per=commands.BucketType.guild, wait=False)
    async def remind_c_lesson(self, ctx):
        """DM eligible pending members an onboarding reminder for today's C lesson."""
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
            LOG.warning("Could not fetch members for C lesson reminders: %s", exc)
            await ctx.send("Couldn't fetch the server members; no reminders sent.")
            return
        targets = [member for member in members
                   if needs_reminder(member, pending.id, new_member.id)]
        if not targets:
            await ctx.send("No pending members need a reminder.")
            return
        try:
            records = await asyncio.wait_for(store.get_members([m.id for m in targets]), timeout=20)
            languages = {str(record["user_id"]): record.get("lang") for record in records}
        except (StoreError, TimeoutError) as exc:
            LOG.warning("Could not load reminder languages; using French: %s", exc)
            languages = {}
        await ctx.send(f"Sending C lesson reminders to up to {len(targets)} pending member(s).")
        sent = blocked = skipped = failed = 0
        for member in targets:
            try:
                current = await ctx.guild.fetch_member(member.id)
            except discord.NotFound:
                skipped += 1
                continue
            except discord.HTTPException as exc:
                failed += 1
                LOG.warning("Could not recheck C lesson reminder for %s: %s", member.id, exc)
                continue
            if not needs_reminder(current, pending.id, new_member.id):
                skipped += 1
                continue
            try:
                text, label = reminder_text(languages.get(str(current.id)))
                view = StartOnboardingView(onboarding)
                view.start.label = label
                await current.send(text, view=view)
                sent += 1
            except discord.Forbidden:
                blocked += 1
            except discord.HTTPException as exc:
                failed += 1
                LOG.warning("C lesson reminder failed for %s: %s", member.id, exc)
            await asyncio.sleep(0.6)
        await ctx.send(f"C lesson reminders: {sent} sent, {blocked} DMs closed, "
                       f"{skipped} no longer eligible, {failed} failed.")


async def setup(bot):
    await bot.add_cog(LessonReminder(bot))
