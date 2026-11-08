"""Give the New Member role to verified people; defaults to a read-only preview."""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

import config  # noqa: E402


def missing_new_member(members, verified_role, new_member_role):
    return [member for member in members if not member.bot
            and verified_role in member.roles and new_member_role not in member.roles]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    intents = discord.Intents.none()
    intents.members = True
    async with discord.Client(intents=intents) as client:
        await client.login(config.BOT_TOKEN)
        guild = await client.fetch_guild(config.GUILD_ID)
        roles = await guild.fetch_roles()
        verified = discord.utils.get(roles, name=config.ROLE_VERIFIED)
        new_member = discord.utils.get(roles, id=config.ROLE_NEW_MEMBER_ID)
        if verified is None or new_member is None:
            raise RuntimeError("Required Verified or New Member role is missing")
        members = [member async for member in guild.fetch_members(limit=None)]
        targets = missing_new_member(members, verified, new_member)
        print(f"Verified people missing New Member: {len(targets)}", flush=True)
        if not args.apply:
            return
        added = failed = 0
        for member in targets:
            try:
                await member.add_roles(new_member, reason="Grant New Member to verified members")
                added += 1
            except discord.HTTPException as exc:
                failed += 1
                print(f"Could not add role to {member.id}: HTTP {exc.status}", flush=True)
            if (added + failed) % 20 == 0:
                print(f"Processed {added + failed}/{len(targets)}", flush=True)
            await asyncio.sleep(0.2)
        print(f"New Member role: {added} added, {failed} failed", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
