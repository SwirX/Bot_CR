"""Inspect or apply the Robotics Club channel access policy.

Run with --apply to write the reviewed plan. A permission-overwrite backup is
written before any changes so an administrator can restore the previous state.
"""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import discord

import config
from cogs import _club_ids as ids

ENTRY_CATEGORY = 1333113499664126012
SUPPORT_CATEGORY = 1333113499664126021
CELL_MEMBERS = (1333140196014686250, 1333141189297307769, 1333141458424954982,
                1333141619708264468, 1333142734428700693, 1333142976162959432,
                ids.IT_MEMBER)
CELL_HEADS = (1333113498720403539, 1333113498720403540, 1333113498720403541,
              1333113498720403543, 1333113498720403544, ids.IT_HEAD)
BUREAU = (ids.ARCHON, ids.PRESIDENT, ids.VICE_PRESIDENT, ids.MANAGER)


def build_plan(guild):
    roles = {role.id: role for role in guild.roles}
    required = (ids.PENDING, ids.VERIFIED, ids.OLD_MEMBER, ids.NEW_MEMBER,
                ids.BOT_DEVELOPER, *CELL_MEMBERS, *CELL_HEADS, *BUREAU)
    missing = [role_id for role_id in required if role_id not in roles]
    if missing:
        raise RuntimeError(f"Missing required roles: {missing}")
    changes = {}

    def set_values(channel, target, **values):
        key = (channel.id, target.id)
        if key not in changes:
            original = channel.overwrites_for(target)
            changes[key] = [channel, target, original,
                            discord.PermissionOverwrite.from_pair(*original.pair())]
        for name, value in values.items():
            setattr(changes[key][3], name, value)

    for channel in guild.channels:
        if not hasattr(channel, "overwrites_for"):
            continue
        parent = getattr(channel, "category_id", None)
        allowed_pending = (channel.id in (ENTRY_CATEGORY, SUPPORT_CATEGORY, ids.RULES, ids.VERIFICATION)
                           or parent == SUPPORT_CATEGORY)
        set_values(channel, roles[ids.PENDING], view_channel=allowed_pending)
        if channel.id in (ids.RULES, ids.VERIFICATION):
            set_values(channel, guild.default_role, view_channel=True, read_message_history=True)
        if channel.id == ids.STAFF_CATEGORY:
            set_values(channel, guild.default_role, view_channel=False)
            for role_id in (*CELL_MEMBERS, *CELL_HEADS, *BUREAU):
                set_values(channel, roles[role_id], view_channel=True)
        if parent == ids.STAFF_CATEGORY:
            set_values(channel, guild.default_role, view_channel=False)
            for role_id in (*CELL_HEADS, *BUREAU):
                set_values(channel, roles[role_id], view_channel=True)
        if channel.id == ids.BUREAU_CATEGORY or parent == ids.BUREAU_CATEGORY:
            set_values(channel, guild.default_role, view_channel=False)
            for role in guild.roles:
                if role.id not in (*BUREAU, guild.id, ids.PENDING) and channel.overwrites_for(role).view_channel is True:
                    set_values(channel, role, view_channel=None)
            for role_id in BUREAU:
                set_values(channel, roles[role_id], view_channel=True)
        if channel.id == ids.BOT_DEVELOPMENT:
            set_values(channel, guild.default_role, view_channel=False)
            for role_id in (ids.IT_HEAD, ids.IT_MEMBER, ids.BOT_DEVELOPER, *BUREAU):
                set_values(channel, roles[role_id], view_channel=True,
                           send_messages=True, read_message_history=True)
        if channel.id == ids.OLD_MEMBERS_VOICE:
            set_values(channel, guild.default_role, view_channel=False, connect=False)
            set_values(channel, roles[ids.VERIFIED], view_channel=True, connect=False)
            for role_id in (ids.OLD_MEMBER, *CELL_MEMBERS, *CELL_HEADS, *BUREAU):
                set_values(channel, roles[role_id], view_channel=True, connect=True)
    return [item for item in changes.values() if item[2].pair() != item[3].pair()]


async def main(apply):
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    client = discord.Client(intents=intents)
    errors = []

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(ids.GUILD)
            if guild is None:
                raise RuntimeError("Club server is unavailable")
            changes = build_plan(guild)
            print(f"Planned {len(changes)} channel permission updates", flush=True)
            for channel, target, old, new in changes:
                print(f"{channel.id} {channel.name} | {target.id} {target.name} | {old.pair()} -> {new.pair()}", flush=True)
            if apply and changes:
                directory = Path.home() / "bot-cr-permission-backups"
                directory.mkdir(mode=0o700, exist_ok=True)
                path = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
                backup = [{"channel": channel.id, "target": target.id,
                           "allow": old.pair()[0].value, "deny": old.pair()[1].value,
                           "existed": target in channel.overwrites}
                          for channel, target, old, _ in changes]
                path.write_text(json.dumps(backup, indent=2))
                print(f"Backup: {path}", flush=True)
                for index, (channel, target, _, new) in enumerate(changes, 1):
                    await channel.set_permissions(target, overwrite=new, reason="Robotics Club access policy")
                    if index % 25 == 0:
                        print(f"Applied {index}/{len(changes)}", flush=True)
                print(f"Applied {len(changes)} updates", flush=True)
        except (discord.HTTPException, RuntimeError, OSError) as exc:
            errors.append(exc)
        finally:
            await client.close()

    await client.start(config.BOT_TOKEN)
    if errors:
        raise RuntimeError(f"Access policy was not fully applied: {errors[0]}") from errors[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
