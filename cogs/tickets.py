"""Private support tickets for club members."""

import asyncio
import logging

import discord
from discord.ext import commands

from cogs import _club_ids as ids
from cogs._ticket_views import (CATEGORY_ID, CloseTicketView, OpenTicketView,
                                STAFF_ROLES, TOPIC_PREFIX, ticket_owner)
from data.store import StoreError, store

LOG = logging.getLogger("bot.tickets")
CHANNEL_KEY = "ticket_panel_channel"
MESSAGE_KEY = "ticket_panel_message"
PANEL_TEXT = ("**Support du club**\n"
              "Besoin d'aide pour la vérification, les cours ou le serveur ? "
              "Cliquez sur le bouton et expliquez votre demande. "
              "Un salon privé sera créé pour vous et l'équipe du club.")


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.locks = {}
        self.open_channels = {}
        self.panel_lock = asyncio.Lock()
        bot.add_view(OpenTicketView(self))
        bot.add_view(CloseTicketView())

    @commands.Cog.listener()
    async def on_ready(self):
        async with self.panel_lock:
            await self.ensure_panel()

    async def ensure_panel(self):
        guild = self.bot.get_guild(ids.GUILD)
        category = guild.get_channel(CATEGORY_ID) if guild else None
        if not isinstance(category, discord.CategoryChannel):
            LOG.error("Support category unavailable")
            return
        try:
            saved_channel = await store.get_setting(CHANNEL_KEY)
            channel = guild.get_channel(int(saved_channel)) if saved_channel else None
            if not isinstance(channel, discord.TextChannel) or channel.category_id != CATEGORY_ID:
                channel = discord.utils.get(category.text_channels, name="support")
            if channel is None:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=True, read_message_history=True, send_messages=False),
                    guild.me: discord.PermissionOverwrite(
                        view_channel=True, read_message_history=True, send_messages=True,
                        manage_channels=True, manage_messages=True),
                }
                channel = await guild.create_text_channel(
                    "support", category=category, overwrites=overwrites,
                    reason="Public support ticket panel")
            await store.set_setting(CHANNEL_KEY, str(channel.id))
            saved_message = await store.get_setting(MESSAGE_KEY)
            message = None
            if saved_message:
                try:
                    message = await channel.fetch_message(int(saved_message))
                except (discord.NotFound, ValueError):
                    pass
            if message is None:
                async for previous in channel.history(limit=100):
                    if previous.author.id == self.bot.user.id and any(
                        getattr(child, "custom_id", None) == "club_ticket_open_v1"
                        for row in previous.components for child in row.children
                    ):
                        message = previous
                        break
            if message is not None and message.author.id == self.bot.user.id:
                await message.edit(content=PANEL_TEXT, view=OpenTicketView(self))
            else:
                message = await channel.send(PANEL_TEXT, view=OpenTicketView(self))
            await store.set_setting(MESSAGE_KEY, str(message.id))
        except (discord.HTTPException, StoreError, ValueError) as exc:
            LOG.error("Could not establish support panel: %s", exc)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        owner = ticket_owner(channel)
        if owner is not None:
            self.open_channels.pop(owner, None)

    async def open_ticket(self, interaction, subject, details):
        guild = interaction.guild
        if guild is None or guild.id != ids.GUILD:
            await interaction.followup.send("Utilisez ce formulaire dans le serveur du club.", ephemeral=True)
            return
        if not subject.strip() or not details.strip():
            await interaction.followup.send("Veuillez renseigner le sujet et votre demande.", ephemeral=True)
            return
        member = guild.get_member(interaction.user.id)
        if member is None:
            await interaction.followup.send("Votre compte est introuvable dans le serveur.", ephemeral=True)
            return
        async with self.locks.setdefault(member.id, asyncio.Lock()):
            category = guild.get_channel(CATEGORY_ID)
            if not isinstance(category, discord.CategoryChannel):
                await interaction.followup.send("Le support est indisponible. Contactez un responsable.", ephemeral=True)
                return
            existing = self.open_channels.get(member.id) or next(
                (channel for channel in category.text_channels if ticket_owner(channel) == member.id), None)
            if existing:
                await interaction.followup.send(f"Vous avez déjà un ticket ouvert : {existing.mention}", ephemeral=True)
                return
            roles = [guild.get_role(role_id) for role_id in STAFF_ROLES]
            if any(role is None for role in roles):
                LOG.error("A ticket staff role is missing")
                await interaction.followup.send("Le support est indisponible. Contactez un responsable.", ephemeral=True)
                return
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                    read_message_history=True, attach_files=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                      read_message_history=True, manage_channels=True),
            }
            for role in roles:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                               read_message_history=True, attach_files=True)
            channel = None
            try:
                channel = await guild.create_text_channel(
                    f"ticket-{member.id}", category=category,
                    topic=f"{TOPIC_PREFIX}{member.id}", overwrites=overwrites,
                    reason=f"Support ticket opened by {member.id}")
                self.open_channels[member.id] = channel
                embed = discord.Embed(title=subject.strip()[:100], description=details.strip()[:1200],
                                      colour=discord.Colour.blurple())
                await channel.send(f"Ticket de {member.mention}. L'équipe peut répondre ici.",
                                   embed=embed, view=CloseTicketView(),
                                   allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                LOG.error("Ticket creation failed for %s: %s", member.id, exc)
                if channel is not None:
                    try:
                        await channel.delete(reason="Incomplete support ticket")
                        self.open_channels.pop(member.id, None)
                    except discord.HTTPException:
                        LOG.error("Could not remove incomplete ticket %s", channel.id)
                await interaction.followup.send("Impossible de créer le ticket. Contactez un responsable.", ephemeral=True)
                return
            await interaction.followup.send(f"Votre ticket est ouvert : {channel.mention}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Tickets(bot))
