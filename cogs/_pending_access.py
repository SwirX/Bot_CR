"""Keep pending members restricted when another role grants channel access."""

import asyncio
import json
import logging

import discord

from cogs import _club_ids as ids
from data.store import StoreError, store

LOG = logging.getLogger("bot.pending_access")
ENTRY_CATEGORY = 1333113499664126012
SUPPORT_CATEGORY = 1333113499664126021
_LOCKS: dict[int, asyncio.Lock] = {}


def allowed_while_pending(channel):
    return (channel.id in (ENTRY_CATEGORY, ids.RULES, ids.VERIFICATION, SUPPORT_CATEGORY)
            or getattr(channel, "category_id", None) == SUPPORT_CATEGORY)


async def _restrict_pending(member):
    if member.bot or not any(role.id == ids.PENDING for role in member.roles):
        return
    if member.guild_permissions.administrator:
        LOG.warning("Pending member %s has Administrator and bypasses channel restrictions", member.id)
        return
    key = f"pending_access.{member.id}"
    try:
        saved = json.loads(await store.get_setting(key) or "{}")
        for channel in member.guild.channels:
            if allowed_while_pending(channel) or not channel.permissions_for(member).view_channel:
                continue
            overwrite = channel.overwrites_for(member)
            cid = str(channel.id)
            if cid not in saved:
                saved[cid] = overwrite.view_channel
                await store.set_setting(key, json.dumps(saved))
            overwrite.view_channel = False
            await channel.set_permissions(member, overwrite=overwrite, reason="Pending club verification")
    except (discord.HTTPException, StoreError, ValueError) as exc:
        LOG.error("Could not restrict pending member %s: %s", member.id, exc)


async def _release_pending(member):
    key = f"pending_access.{member.id}"
    try:
        saved = json.loads(await store.get_setting(key) or "{}")
        for cid, previous in list(saved.items()):
            channel = member.guild.get_channel(int(cid))
            if channel is not None:
                overwrite = channel.overwrites_for(member)
                if overwrite.view_channel is False:
                    overwrite.view_channel = previous
                    await channel.set_permissions(member, overwrite=None if overwrite.is_empty() else overwrite,
                                                  reason="Club verification completed")
            del saved[cid]
            await store.set_setting(key, json.dumps(saved))
    except (discord.HTTPException, StoreError, ValueError) as exc:
        LOG.error("Could not restore access for verified member %s: %s", member.id, exc)


async def restrict_pending(member):
    async with _LOCKS.setdefault(member.id, asyncio.Lock()):
        current = member.guild.get_member(member.id) or member
        await _restrict_pending(current)


async def release_pending(member):
    async with _LOCKS.setdefault(member.id, asyncio.Lock()):
        await _release_pending(member)
