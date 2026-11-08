"""Inspect or perform the requested onboarding and welcome-message follow-up."""

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import discord  # noqa: E402
from appwrite.query import Query  # noqa: E402

import config  # noqa: E402
from data.appwrite_client import build_tables_client  # noqa: E402

MENTION = re.compile(r"<@!?(\d+)>")
WELCOME_TITLE = "🎉 Welcome to the Robotics Club's Server! 🎉"


async def missing_name_members(members):
    humans = [member for member in members if not member.bot]
    tables = build_tables_client()
    records = {}
    for start in range(0, len(humans), 25):
        ids = [str(member.id) for member in humans[start:start + 25]]
        result = await asyncio.to_thread(
            tables.list_rows, config.APPWRITE_DATABASE_ID, "discord_data",
            queries=[Query.equal("$id", ids), Query.limit(25)],
        )
        records.update({row.id: row.data for row in result.rows})
    return [member for member in humans
            if not str(records.get(str(member.id), {}).get("real_name") or "").strip()]


def invite_view():
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="Start onboarding", style=discord.ButtonStyle.success,
        custom_id="onboarding_start",
    ))
    return view


async def send_invitations(members, checkpoint):
    sent_ids = set(checkpoint.read_text().splitlines()) if checkpoint.exists() else set()
    sent = blocked = failed = 0
    for member in members:
        if str(member.id) in sent_ids:
            continue
        try:
            await member.send(
                "Hello! We don't have your full name on file. Please use the button below "
                "to provide your name and gender. Your birthday is optional and lets us "
                "send you a birthday message.",
                view=invite_view(),
            )
            fd = os.open(checkpoint, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as file:
                file.write(f"{member.id}\n")
                file.flush()
                os.fsync(file.fileno())
            sent += 1
        except discord.Forbidden:
            blocked += 1
        except discord.HTTPException:
            failed += 1
        await asyncio.sleep(0.6)
    return sent, blocked, failed, len(sent_ids)


async def repair_welcomes(client, guild, member_ids, *, apply):
    channels = await guild.fetch_channels()
    channel = discord.utils.get(channels, name=config.CHANNEL_WELCOME)
    if channel is None:
        raise RuntimeError("Welcome channel not found")
    candidates = repaired = failed = 0
    names = {}
    async for message in channel.history(limit=None):
        if message.author.id != client.user.id or not message.embeds:
            continue
        embed = message.embeds[0]
        if embed.title != WELCOME_TITLE:
            continue
        match = MENTION.search(embed.description or "")
        if not match or int(match.group(1)) in member_ids:
            continue
        candidates += 1
        try:
            user_id = int(match.group(1))
            if user_id not in names:
                user = await client.fetch_user(user_id)
                names[user_id] = discord.utils.escape_mentions(
                    discord.utils.escape_markdown(user.global_name or user.name))
            if apply:
                embed.description = embed.description.replace(match.group(0), names[user_id], 1)
                await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                repaired += 1
        except discord.HTTPException:
            failed += 1
    return candidates, repaired, failed


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-missing", action="store_true")
    parser.add_argument("--repair-welcomes", action="store_true")
    parser.add_argument("--checkpoint", type=Path, default=Path.home() / ".bot-cr-name-followup")
    args = parser.parse_args()
    intents = discord.Intents.none()
    intents.members = True
    async with discord.Client(intents=intents) as client:
        await client.login(config.BOT_TOKEN)
        guild = await client.fetch_guild(config.GUILD_ID)
        members = [member async for member in guild.fetch_members(limit=None)]
        targets = await missing_name_members(members)
        print(f"Human members missing a name: {len(targets)}", flush=True)
        candidates, repaired, failed = await repair_welcomes(
            client, guild, {member.id for member in members}, apply=args.repair_welcomes)
        print(f"Welcome messages: {candidates} need repair; {repaired} repaired; {failed} failed",
              flush=True)
        if args.send_missing:
            sent, blocked, failed, skipped = await send_invitations(targets, args.checkpoint)
            print(f"Private messages: {sent} sent; {blocked} blocked; {failed} failed; "
                  f"{skipped} already recorded", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
