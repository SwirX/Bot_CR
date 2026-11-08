"""Resolve a DM interaction to its member in the club server."""

import logging

import discord

import config

LOG = logging.getLogger("bot.member_identity")


async def member_for_interaction(interaction: discord.Interaction) -> discord.Member | None:
    guild = interaction.guild
    if guild is None and config.GUILD_ID:
        guild = interaction.client.get_guild(config.GUILD_ID)
        if guild is None:
            LOG.warning("Configured guild %s is unavailable for name submission", config.GUILD_ID)
            return None
    if guild is None:
        cached_guilds = [candidate for candidate in interaction.client.guilds
                         if candidate.get_member(interaction.user.id) is not None]
        if len(cached_guilds) == 1:
            guild = cached_guilds[0]
        elif len(interaction.client.guilds) == 1:
            guild = interaction.client.guilds[0]
    if guild is None:
        LOG.warning("Cannot identify a guild for name submission by %s", interaction.user.id)
        return None

    member = guild.get_member(interaction.user.id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(interaction.user.id)
    except (discord.NotFound, discord.Forbidden) as exc:
        LOG.warning("Cannot find member %s in guild %s: %s",
                    interaction.user.id, guild.id, exc)
    except discord.HTTPException as exc:
        LOG.warning("Failed to fetch member %s from guild %s: %s",
                    interaction.user.id, guild.id, exc)
    return None
