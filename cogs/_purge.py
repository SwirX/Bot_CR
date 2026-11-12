"""Date parsing and cross-channel message deletion."""

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

from cogs._perms import is_bot_admin
from cogs.message_expiry import BUREAU_CATEGORY_ID

ZONE = ZoneInfo("Africa/Casablanca")
MESSAGE_CHANNELS = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread)


def day_bounds(value, now=None):
    text = value.strip().lower()
    today = (now or datetime.now(ZONE)).astimezone(ZONE).date()
    stamp = re.fullmatch(r"<t:(\d+)(?::[tTdDfFR])?>", text, re.IGNORECASE)
    ago = re.fullmatch(r"(\d+)\s+(day|days|week|weeks)\s+ago", text)
    if stamp:
        day = datetime.fromtimestamp(int(stamp.group(1)), timezone.utc).astimezone(ZONE).date()
    elif text in ("today", "yesterday", "today last week", "last week"):
        days = {"today": 0, "yesterday": 1, "today last week": 7, "last week": 7}[text]
        day = today - timedelta(days=days)
    elif ago:
        day = today - timedelta(days=int(ago.group(1)) * (7 if ago.group(2).startswith("week") else 1))
    else:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    return (datetime.combine(day, time.min, ZONE),
            datetime.combine(day + timedelta(days=1), time.min, ZONE))


def allowed(channel, author, bot_member):
    if not isinstance(channel, MESSAGE_CHANNELS):
        return False
    user = channel.permissions_for(author)
    bot = channel.permissions_for(bot_member)
    if isinstance(channel, discord.Thread) and channel.is_private():
        member_ids = {member.id for member in channel.members}
        if not user.manage_threads and author.id not in member_ids:
            return False
        if not bot.manage_threads and bot_member.id not in member_ids:
            return False
    return (user.view_channel and user.read_message_history
            and (user.manage_messages or is_bot_admin(author))
            and bot.view_channel and bot.read_message_history and bot.manage_messages)


def outside_bureau(channel):
    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    return parent is not None and parent.category_id != BUREAU_CATEGORY_ID


async def all_targets(guild, author):
    targets = list(guild.channels) + list(guild.threads)
    failures = 0
    for parent in guild.channels:
        if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
            continue
        if not outside_bureau(parent) or not parent.permissions_for(author).view_channel:
            continue
        try:
            async for thread in parent.archived_threads(limit=None):
                targets.append(thread)
            if isinstance(parent, discord.TextChannel):
                private = parent.permissions_for(author).manage_threads
                async for thread in parent.archived_threads(private=True, joined=not private, limit=None):
                    targets.append(thread)
        except discord.HTTPException:
            failures += 1
    unique = {channel.id: channel for channel in targets
              if isinstance(channel, MESSAGE_CHANNELS) and outside_bureau(channel)}
    return list(unique.values()), failures


async def purge(targets, author, bot_member, number, start, end, member, command_message):
    selected = []
    deleted = 0
    skipped = 0
    failed = 0
    for channel in targets:
        if not allowed(channel, author, bot_member):
            skipped += 1
            continue
        def matches(message):
            return ((member is None or message.author.id == member.id)
                    and (command_message is None or message.id != command_message.id))
        try:
            before = end
            if command_message and channel.id == command_message.channel.id:
                if before is None or command_message.created_at < before:
                    before = command_message
            if number is None:
                deleted += len(await channel.purge(limit=None, before=before, after=start, check=matches))
            else:
                found = 0
                async for message in channel.history(limit=None, before=before, after=start):
                    if matches(message):
                        selected.append(message)
                        found += 1
                        if found >= number:
                            break
        except discord.HTTPException:
            failed += 1
    if number is not None:
        for message in sorted(selected, key=lambda m: m.id, reverse=True)[:number]:
            try:
                await message.delete()
                deleted += 1
            except discord.HTTPException:
                failed += 1
    return deleted, skipped, failed
