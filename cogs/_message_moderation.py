"""Moderation actions for individual messages."""

import logging

import discord

from cogs._perms import is_bot_admin

LOG = logging.getLogger("bot.message_moderation")


async def delete_message(ctx, message_id: str, channel=None):
    target = channel or ctx.channel
    if not hasattr(target, "fetch_message"):
        await ctx.send("Choose a text channel or thread.", ephemeral=True)
        return
    if not is_bot_admin(ctx.author):
        perms = target.permissions_for(ctx.author)
        if not (perms.view_channel and perms.read_message_history and perms.manage_messages):
            await ctx.send("You need permission to view, read history and manage messages "
                           "in that channel.", ephemeral=True)
            return
    raw_id = message_id.strip()
    if (not raw_id.isascii() or not raw_id.isdecimal() or len(raw_id) > 20
            or not 0 < int(raw_id) < 2**64):
        await ctx.send("Enter a valid numeric message ID.", ephemeral=True)
        return
    await ctx.defer(ephemeral=True)
    try:
        message = await target.fetch_message(int(raw_id))
        await message.delete(reason=f"Requested by {ctx.author} ({ctx.author.id})")
    except discord.NotFound:
        await ctx.send("I couldn't find that message in the selected channel.", ephemeral=True)
        return
    except discord.Forbidden:
        await ctx.send("I can't access or delete that message. Check my channel permissions.",
                       ephemeral=True)
        return
    except discord.HTTPException as exc:
        LOG.warning("Message deletion failed in channel %s: %s", target.id, exc)
        await ctx.send("Discord couldn't delete that message. Please try again.", ephemeral=True)
        return
    LOG.info("Message %s deleted in channel %s by %s", raw_id, target.id, ctx.author.id)
    await ctx.send("Message deleted.", ephemeral=True)
