"""Confirmation and storage for removing finished meeting records."""

import asyncio

import discord
from appwrite.exception import AppwriteException
from appwrite.query import Query

from cogs._perms import is_meeting_report_viewer
from data.store import StoreError, is_missing, store


async def delete_finished_meeting(meeting_id: str) -> None:
    meeting = await store.get_meeting(meeting_id)
    if meeting is None:
        raise StoreError("This meeting no longer exists.")
    if meeting.get("live"):
        raise StoreError("End the meeting before deleting it.")
    client, database = store._raw()

    async def delete_row(table, row_id):
        try:
            await asyncio.to_thread(client.delete_row, database, table, row_id)
        except AppwriteException as exc:
            if not is_missing(exc):
                raise StoreError(f"Could not delete {table} record: {exc}") from exc

    while True:
        rows = await store._list("meeting_sessions", [Query.equal("meeting", meeting_id), Query.limit(25)])
        if not rows:
            break
        for row in rows:
            await delete_row("meeting_sessions", row["$id"])
    await delete_row("bot_settings", f"meeting_side.{meeting_id}")
    await delete_row("meetings", meeting_id)


class DeleteMeetingView(discord.ui.View):
    def __init__(self, meeting: dict, owner_id: int):
        super().__init__(timeout=120)
        self.meeting = meeting
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id and is_meeting_report_viewer(interaction.user):
            return True
        await interaction.response.send_message("Only the authorized requester can confirm.", ephemeral=True)
        return False

    @discord.ui.button(label="Delete meeting and attendance", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            await delete_finished_meeting(self.meeting["id"])
        except StoreError as exc:
            await interaction.edit_original_response(content=f"Deletion failed: {exc}. Some attendance rows may already have been deleted; retry.", view=None)
            return
        await interaction.edit_original_response(content="Meeting and attendance records deleted.", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(content="Deletion cancelled.", view=None)
        self.stop()
