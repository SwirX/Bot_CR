"""Server voice mute batches with a persisted undo list."""

import asyncio
import json
import logging

import discord

from cogs._perms import is_bot_admin
from data.store import StoreError, store

LOG = logging.getLogger("bot.voice_mute")
_LOCKS: dict[int, asyncio.Lock] = {}


def _channel(ctx, channel):
    return channel or getattr(getattr(ctx.author, "voice", None), "channel", None)


def _allowed(ctx, channel):
    return (is_bot_admin(ctx.author) or channel.permissions_for(ctx.author).mute_members)


def _key(channel):
    return f"voicebatch.{channel.id}"


async def _load(channel):
    value = await store.get_setting(_key(channel))
    try:
        ids = json.loads(value or "[]")
        if not isinstance(ids, list) or any(not str(uid).isdecimal() for uid in ids):
            raise ValueError("invalid voice mute batch")
        return [str(uid) for uid in ids]
    except (ValueError, TypeError) as exc:
        raise StoreError("Saved voice mute batch is invalid; contact an administrator.") from exc


async def _save(channel, ids):
    await store.set_setting(_key(channel), json.dumps(ids))


async def _prepare(ctx, channel):
    channel = _channel(ctx, channel)
    if channel is None:
        await ctx.send("Join a voice channel or choose one with `channel`.", ephemeral=True)
        return None
    if not _allowed(ctx, channel):
        await ctx.send("You need Mute Members in that voice channel.", ephemeral=True)
        return None
    if not channel.permissions_for(ctx.guild.me).mute_members:
        await ctx.send("I need Mute Members in that voice channel.", ephemeral=True)
        return None
    return channel


async def mute_all(ctx, channel=None, exclude=None):
    channel = await _prepare(ctx, channel)
    if channel is None:
        return
    async with _LOCKS.setdefault(channel.id, asyncio.Lock()):
        try:
            saved = await _load(channel)
            if saved:
                await ctx.send("This channel already has a mute batch. Undo it first.", ephemeral=True)
                return
            targets = [member for member in channel.members if not member.bot
                       and member.voice and not member.voice.mute
                       and (exclude is None or member.id != exclude.id)]
            muted = failed = 0
            for member in targets:
                saved.append(str(member.id))
                await _save(channel, saved)
                try:
                    await member.edit(mute=True, reason=f"Bulk voice mute by {ctx.author.id}")
                    muted += 1
                except discord.HTTPException as exc:
                    LOG.warning("Voice mute failed for %s: %s", member.id, exc)
                    saved.pop()
                    await _save(channel, saved)
                    failed += 1
        except StoreError as exc:
            LOG.error("Could not persist voice mute batch: %s", exc)
            await ctx.send("Could not save the undo list; stopped. Use undo for any members already muted.", ephemeral=True)
            return
    await ctx.send(f"Server-muted {muted} member(s) in {channel.mention}; {failed} failed. "
                   "Use `/meeting undomute` to undo this batch.", ephemeral=True)


async def undo_mute(ctx, channel=None):
    channel = await _prepare(ctx, channel)
    if channel is None:
        return
    async with _LOCKS.setdefault(channel.id, asyncio.Lock()):
        try:
            saved = await _load(channel)
            if not saved:
                await ctx.send("There is no saved mute batch for that channel.", ephemeral=True)
                return
            restored = 0
            remaining = list(saved)
            for uid in saved:
                member = ctx.guild.get_member(int(uid))
                voice = getattr(member, "voice", None)
                current = getattr(voice, "channel", None)
                if member and current and voice.mute:
                    if not _allowed(ctx, current) or not current.permissions_for(ctx.guild.me).mute_members:
                        continue
                    try:
                        await member.edit(mute=False, reason=f"Undo bulk voice mute by {ctx.author.id}")
                        restored += 1
                    except discord.HTTPException as exc:
                        LOG.warning("Voice unmute failed for %s: %s", uid, exc)
                        continue
                remaining.remove(uid)
                await _save(channel, remaining)
        except StoreError as exc:
            LOG.error("Could not update voice undo batch: %s", exc)
            await ctx.send("Could not update the undo list. Retry the undo command.", ephemeral=True)
            return
    await ctx.send(f"Restored {restored} member(s) from the batch; {len(remaining)} still need attention.", ephemeral=True)
