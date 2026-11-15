"""Persistent support ticket controls."""

import discord

from cogs import _club_ids as ids

STAFF_ROLES = frozenset({
    1333140196014686250, 1333141189297307769, 1333141458424954982,
    1333141619708264468, 1333142734428700693, 1333142976162959432,
    ids.IT_MEMBER, 1333113498720403539, 1333113498720403540,
    1333113498720403541, 1333113498720403543, 1333113498720403544,
    ids.IT_HEAD, ids.ARCHON, ids.PRESIDENT, ids.VICE_PRESIDENT, ids.MANAGER,
})
TOPIC_PREFIX = "robotics-support-owner:"
CATEGORY_ID = ids.SUPPORT_CATEGORY


def ticket_owner(channel):
    topic = getattr(channel, "topic", "") or ""
    if not topic.startswith(TOPIC_PREFIX):
        return None
    owner = topic[len(TOPIC_PREFIX):].split(" ", 1)[0]
    return int(owner) if owner.isdecimal() else None


def may_close(member, channel):
    if getattr(channel, "category_id", None) != CATEGORY_ID or ticket_owner(channel) is None:
        return False
    return (member.id == ticket_owner(channel)
            or member.guild_permissions.administrator
            or any(role.id in STAFF_ROLES for role in member.roles))


class TicketForm(discord.ui.Modal, title="Contacter le support"):
    subject = discord.ui.TextInput(label="Sujet", placeholder="Ex. verification, cours, compte", max_length=100)
    details = discord.ui.TextInput(label="Comment pouvons-nous vous aider ?", style=discord.TextStyle.paragraph,
                                   max_length=1200)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.open_ticket(interaction, self.subject.value, self.details.value)


class OpenTicketView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Créer un ticket", style=discord.ButtonStyle.primary,
                       emoji="🎫", custom_id="club_ticket_open_v1")
    async def open(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.guild is None or interaction.guild.id != ids.GUILD:
            await interaction.response.send_message("Utilisez ce bouton dans le serveur du club.", ephemeral=True)
            return
        await interaction.response.send_modal(TicketForm(self.cog))


class ConfirmCloseView(discord.ui.View):
    def __init__(self, requester_id):
        super().__init__(timeout=60)
        self.requester_id = requester_id

    @discord.ui.button(label="Fermer et supprimer", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.requester_id or not may_close(interaction.user, interaction.channel):
            await interaction.response.send_message("Vous ne pouvez pas fermer ce ticket.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.channel.delete(reason=f"Support ticket closed by {interaction.user.id}")
        except discord.HTTPException:
            await interaction.followup.send("Impossible de fermer le ticket. Contactez un responsable.", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Cette confirmation appartient à une autre personne.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Fermeture annulée.", view=None)
        self.stop()


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fermer le ticket", style=discord.ButtonStyle.danger,
                       emoji="🔒", custom_id="club_ticket_close_v1")
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.guild is None or interaction.guild.id != ids.GUILD or not may_close(interaction.user, interaction.channel):
            await interaction.response.send_message("Vous ne pouvez pas fermer ce ticket.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Fermer ce ticket supprimera le salon et ses messages. Continuer ?",
            view=ConfirmCloseView(interaction.user.id), ephemeral=True)
