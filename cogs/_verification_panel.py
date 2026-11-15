"""Keep the public verification form available across bot restarts."""

import logging

import discord

from cogs import _club_ids as ids
from data.store import StoreError, store

LOG = logging.getLogger("bot.verification_panel")
KEY = "verification_panel_message"
TEXT = ("**Verification du club**\n"
        "Cliquez sur le bouton pour renseigner votre nom complet, votre genre "
        "et, si vous le souhaitez, votre date de naissance. "
        "Votre pseudo et vos rôles seront mis à jour après validation.")


async def ensure_panel(bot, make_view):
    guild = bot.get_guild(ids.GUILD)
    channel = guild.get_channel(ids.VERIFICATION) if guild else None
    if channel is None:
        LOG.warning("Verification channel unavailable")
        return
    try:
        saved = await store.get_setting(KEY)
        if saved:
            try:
                message = await channel.fetch_message(int(saved))
                if message.author.id == bot.user.id:
                    await message.edit(content=TEXT, view=make_view())
                    return
            except (discord.NotFound, ValueError):
                pass
        async for message in channel.history(limit=100):
            if message.author.id == bot.user.id and any(
                child.custom_id == "onboarding_start"
                for row in message.components for child in row.children
            ):
                await message.edit(content=TEXT, view=make_view())
                await store.set_setting(KEY, str(message.id))
                return
        message = await channel.send(TEXT, view=make_view())
        await store.set_setting(KEY, str(message.id))
    except (discord.HTTPException, StoreError) as exc:
        LOG.error("Could not ensure verification panel: %s", exc)
