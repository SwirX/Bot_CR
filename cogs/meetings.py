"""Meetings: tracked club meetings and private voice rooms.

Two independent features share the ``/meeting`` group.

**Tracked meetings** (``/meeting start|end|lock|unlock|list|last``) record a
club meeting in an existing voice channel: the channel's permissions are
narrowed to the chosen audience for the duration, every join/leave is written
to the hub, and the resulting attendance is exported as CSV. See
:mod:`cogs._meetings` for the permission snapshot/restore and attendance
helpers.

**Private rooms** (``/meeting create|endroom``) spin up a throwaway voice
channel that only the creator and the tagged members can see, and that deletes
itself the moment it empties. These predate attendance tracking and are
registered in the Appwrite settings so they survive bot restarts.

The two coexist because a private room is a *place*, while a tracked meeting is
an *event with a report*. The room's ``end`` subcommand was renamed to
``endroom`` when attendance tracking claimed ``end``.
"""

import json
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

import config
from cogs import _meetings as meetlib
from cogs._perms import is_meeting_admin, require_meeting_admin
from cogs._ui import OwnerView, close_panel, select_value
from data.store import store
from data.store import StoreError

LOG = logging.getLogger("bot.meetings")

SETTINGS_KEY = "meetings"
MAX_MEMBERS = 15  # sane cap on tagged members per private room

# Rows the absentee card grid may occupy. Discord allows five buttons per row
# and 25 components per view, so two rows leaves room for the pager and the
# back/close pair below them.
_CARD_ROWS = 2


class MeetingListView(OwnerView, discord.ui.View):
    """``/meeting list``: a dropdown over meeting ids that opens the report.

    The dropdown carries up to :data:`config.MEETING_MAX_PICKER_OPTIONS`
    entries because Discord rejects an app command with more than 25 options
    and silently breaks the whole command if you try.
    """

    def __init__(self, cog: "Meetings", meetings: list[dict], *,
                 user: discord.abc.User, timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.cog, self.meetings = cog, meetings
        self.user_id = user.id
        self.embed = meetlib.list_embed(meetings)
        self.by_id = {str(m["id"]): m for m in meetings}

        limit = max(1, config.MEETING_MAX_PICKER_OPTIONS)
        options = []
        for m in meetings[:limit]:
            when = meetlib.friendly_time(m.get("started_at"))
            flags = "🔴 " if m.get("live") else ""
            flags += "🔒 " if m.get("locked") else ""
            options.append(discord.SelectOption(
                value=str(m["id"]),
                label=f"{flags}{m.get('title') or 'Meeting'}"[:100],
                description=f"{when} · {meetlib.meeting_length(m)}"[:100],
            ))
        picker = discord.ui.Select(
            placeholder="🔎 Pick a meeting to see its attendance…",
            min_values=1, max_values=1, options=options, row=0)
        picker.callback = self._on_pick
        self.add_item(picker)

    def _owner_deny_message(self, _interaction) -> str:
        return ("🔒 This list belongs to the command author — run "
                "`/meeting list` yourself to use it.")

    async def _on_pick(self, interaction: discord.Interaction):
        if not await self._owned(interaction):
            return
        meeting = self.by_id.get(select_value(interaction))
        if meeting is None:
            # The row is gone from the hub between the panel being sent and the
            # pick -- a deleted meeting, not a broken command.
            await interaction.response.send_message(
                "⚠️ That meeting is no longer on record.", ephemeral=True)
            return
        view = MeetingStatsView(self.cog, meeting, user=interaction.user,
                                back=self)
        embed, view = await view.interaction_setup()
        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except discord.HTTPException as exc:
            LOG.error("Meetings: report drill-down failed: %s", exc)

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.secondary, row=1)
    async def close(self, interaction: discord.Interaction,
                    _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        await close_panel(interaction, text="✖️ closed.")

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class MeetingStatsView(OwnerView, discord.ui.View):
    """The report hub: stats, the CSV, and the way to the absentee page.

    Owner-scoped, like the other panels in the bot — a meeting report names
    absentees and invites yellow cards, so it should not be a shared panel in a
    public channel. :attr:`return_to` is set by ``/meeting list`` so the back
    button lands on the picker instead of closing.
    """

    def __init__(self, cog: "Meetings", meeting: dict, *,
                 user: discord.abc.User, back: discord.ui.View | None = None,
                 timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.cog, self.meeting = cog, meeting
        self.user_id = user.id
        self.return_to = back
        self.absent_button.label = "🚫 Absentees (0)"

    def _owner_deny_message(self, _interaction) -> str:
        return ("🔒 This report belongs to the command author — run "
                "`/meeting last` yourself to see it.")

    async def interaction_setup(self) -> tuple[discord.Embed, "MeetingStatsView"]:
        """Build the stats page and size the absentee button from real data."""
        meeting = self.meeting
        sessions = await self.cog._sessions(meeting["id"])
        totals = meetlib.rollup(sessions, ended_at=meeting.get("ended_at") or "",
                                granted=meeting.get("granted") or [])
        absent = meetlib.absentee_ids(meeting, sessions)
        self.absent_button.label = f"🚫 Absentees ({len(absent)})"
        # Nobody absent means there is nothing on the page to act on.
        self.absent_button.disabled = not absent
        return meetlib.stats_embed(meeting, totals, absent), self

    @discord.ui.button(emoji="📄", style=discord.ButtonStyle.primary,
                       label="Attendance CSV")
    async def csv_button(self, interaction: discord.Interaction,
                         _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        meeting = self.meeting
        sessions = await self.cog._sessions(meeting["id"])
        totals = meetlib.rollup(sessions, ended_at=meeting.get("ended_at") or "",
                                granted=meeting.get("granted") or [])
        absent = meetlib.absentee_ids(meeting, sessions)
        attachment = meetlib.csv_file(meeting, sessions, totals=totals,
                                      absentees_list=absent)
        try:
            await interaction.response.send_message(
                f"📄 Attendance CSV for **{meeting.get('title') or 'the meeting'}** "
                f"— one row per join, plus per-member totals.",
                file=attachment, ephemeral=True)
        except discord.HTTPException as exc:
            LOG.error("Meetings: CSV send failed for %s: %s", meeting["id"], exc)
            await self._notify(interaction, f"⚠️ Couldn't build the CSV: {exc}")

    @discord.ui.button(emoji="🚫", style=discord.ButtonStyle.secondary,
                       label="🚫 Absentees (0)")
    async def absent_button(self, interaction: discord.Interaction,
                            _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        # A separate page on purpose: the yellow-card buttons are one per
        # absentee and don't fit alongside the stats embed.
        view = MeetingAbsenteesView(self.cog, self.meeting,
                                    user=interaction.user, back=self)
        embed = await self.cog._absentees_embed(self.meeting, view)
        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except discord.HTTPException as exc:
            LOG.error("Meetings: absentee page failed: %s", exc)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        if self.return_to is None:
            await self._notify(interaction, "↩️ Use `/meeting list` to pick a meeting.")
            return
        try:
            await interaction.response.edit_message(
                embed=self.return_to.embed, view=self.return_to)
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.secondary, row=1)
    async def close(self, interaction: discord.Interaction,
                    _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        await close_panel(interaction, text="✖️ closed.")

    async def _notify(self, interaction: discord.Interaction, text: str) -> None:
        """Ephemeral notice for a failure that must not replace the page."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class MeetingAbsenteesView(OwnerView, discord.ui.View):
    """The absentee page: one 🟨 yellow-card button per expected-but-absent member.

    Paged at :data:`config.MEETING_ABSENTEE_PAGE_SIZE` because a club-wide
    meeting can have forty absentees and Discord allows five buttons per row.
    Issuance goes through the same ``warnings`` write as ``/warn``, so a card
    issued here and one issued by hand are the same record and both show up on
    the member's profile.
    """

    def __init__(self, cog: "Meetings", meeting: dict, *,
                 user: discord.abc.User, back: MeetingStatsView,
                 timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.cog, self.meeting, self.stats_view = cog, meeting, back
        self.user_id = user.id
        self.page = 0
        self.page_size = min(max(1, config.MEETING_ABSENTEE_PAGE_SIZE),
                             _CARD_ROWS * 5)
        self.issued: set[int] = set()
        self._grid: list[discord.ui.Button] = []

    def _owner_deny_message(self, _interaction) -> str:
        return ("🔒 This list belongs to the command author — run "
                "`/meeting last` yourself to use it.")

    def build_grid(self, uids: list[str], names: dict[str, str]) -> None:
        """(Re)build the card buttons for the current page.

        Rows are explicit rather than left to discord.py's auto-placement:
        auto-placement packs five per row silently and only raises once the view
        exceeds 25 items, so a 10-card page plus 5 navigation buttons sits
        exactly on that ceiling and a slightly larger page fails at send time
        instead of here. Assigning ``row = i // 5`` keeps the cards above the
        pager rows no matter how the page size is configured.

        ``MEETING_ABSENTEE_PAGE_SIZE`` is clamped to :data:`_CARD_ROWS * 5` so a
        misconfigured value degrades to fewer absentees per page rather than to
        a command that fails to send.
        """
        for button in self._grid:
            self.remove_item(button)
        self._grid = []

        size = min(max(1, config.MEETING_ABSENTEE_PAGE_SIZE), _CARD_ROWS * 5)
        self.page_size = size
        start = self.page * size
        for offset, uid in enumerate(uids[start:start + size]):
            label = (names.get(uid) or f"Member {uid}")[:80]
            button = discord.ui.Button(
                style=discord.ButtonStyle.danger,
                label=f"🟨 {label}"[:80],
                disabled=int(uid) in self.issued,
                custom_id=f"card:{uid}",
                row=offset // 5,
            )
            button.callback = self._make_card_callback(int(uid), label)
            self.add_item(button)
            self._grid.append(button)

        self._sync_pager(len(uids))

    def _sync_pager(self, total: int) -> None:
        pages = max(1, -(-total // self.page_size))
        self.page = min(self.page, pages - 1)
        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= pages - 1
        self.page_label.label = f"{self.page + 1}/{pages}"

    def _make_card_callback(self, uid: int, label: str):
        async def callback(interaction: discord.Interaction, button: discord.ui.Button):
            if not await self._owned(interaction):
                return
            issued, error = await self.cog._issue_yellow_card(
                uid, interaction.user, self.meeting.get("title") or "")
            if error:
                await self._ephemeral(interaction, f"⚠️ {error}")
                return
            self.issued.add(uid)
            # Disable just this card rather than re-rendering: the rest of the
            # page is unchanged and a rebuild would drop the user's scroll.
            button.disabled = True
            button.label = f"✅ {label}"[:80]
            try:
                await interaction.response.edit_message(view=self)
            except discord.HTTPException:
                pass
            await self._ephemeral(
                interaction,
                f"🟨 Yellow card issued to **{label}** for missing "
                f"**{self.meeting.get('title') or 'the meeting'}**.")

        return callback

    async def _ephemeral(self, interaction: discord.Interaction, text: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=3)
    async def prev_page(self, interaction: discord.Interaction,
                        _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        self.page = max(0, self.page - 1)
        await self._rerender(interaction)

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="1/1",
                       disabled=True, row=3)
    async def page_label(self, _interaction: discord.Interaction,
                         _button: discord.ui.Button):
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, row=3)
    async def next_page(self, interaction: discord.Interaction,
                        _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        self.page += 1
        await self._rerender(interaction)

    @discord.ui.button(emoji="📊", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        """◀️ back to the stats page — the absentee page is a child of it."""
        if not await self._owned(interaction):
            return
        embed, view = await self.stats_view.interaction_setup()
        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.secondary, row=4)
    async def close(self, interaction: discord.Interaction,
                    _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        await close_panel(interaction, text="✖️ closed.")

    async def _rerender(self, interaction: discord.Interaction) -> None:
        uids, names = await self.cog._absent_entries(self.meeting)
        self.build_grid(uids, names)
        embed = meetlib.absentees_embed(self.meeting, uids, names)
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class Meetings(commands.Cog):
    """Club meetings with attendance tracking, plus private voice rooms."""

    def __init__(self, bot):
        self.bot = bot
        # channel_id (int) -> {"owner": int, "members": [int, ...]}
        self.meetings: dict[int, dict] = {}

    # ── persistence ──────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        await self.bot.wait_until_ready()
        await self._load_meetings()

    async def _load_meetings(self) -> None:
        """Restore the room registry from the store after a restart."""
        try:
            raw = await store.get_setting(SETTINGS_KEY)
        except StoreError as exc:
            LOG.error("Meetings: could not load persisted rooms: %s", exc)
            return
        if not raw:
            return
        try:
            data = json.loads(raw)
        except ValueError:
            LOG.error("Meetings: stored registry is corrupt; clearing it.")
            data = {}
        if not isinstance(data, dict):
            data = {}
        self.meetings = {
            int(cid): meta for cid, meta in data.items() if str(cid).isdigit() and isinstance(meta, dict)
        }
        # Rooms that vanished while we were offline are dropped from the registry.
        existing = {ch.id for guild in self.bot.guilds for ch in guild.voice_channels}
        gone = [cid for cid in self.meetings if cid not in existing]
        for cid in gone:
            self.meetings.pop(cid, None)
        if gone or len(self.meetings) != len(data):
            await self._save_meetings()
        LOG.info("Meetings: %d private room(s) restored", len(self.meetings))

    async def _save_meetings(self) -> None:
        try:
            await store.set_setting(SETTINGS_KEY, json.dumps(self.meetings))
        except StoreError as exc:
            LOG.error("Meetings: could not persist room registry: %s", exc)

    # ── helpers ──────────────────────────────────────────────────────
    def _find_channel(self, channel_id: int):
        for guild in self.bot.guilds:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                return channel
        return None

    async def _grant_access(self, channel: discord.VoiceChannel, member: discord.Member) -> None:
        await channel.set_permissions(
            member, view_channel=True, connect=True, speak=True
        )

    async def _destroy_meeting(self, channel_id: int) -> None:
        """Delete a room and drop it from the registry (empty rooms only)."""
        self.meetings.pop(channel_id, None)
        await self._save_meetings()
        channel = self._find_channel(channel_id)
        if channel is None:
            return
        try:
            await channel.delete(reason="Meeting room emptied — auto-cleaned")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            LOG.info("Meetings: cleanup of %s skipped: %s", channel_id, exc)

    @commands.hybrid_group(
        name="meeting",
        description="Club meetings with attendance tracking, and private voice rooms.")
    @commands.guild_only()
    async def meeting(self, ctx):
        """Parent group — prints usage when invoked without a subcommand."""
        if ctx.invoked_subcommand is None:
            await ctx.send(
                "🔒 **Meetings**\n"
                "`/meeting start` — start a tracked meeting in a voice channel\n"
                "`/meeting end` — end the running meeting\n"
                "`/meeting list` — browse past meetings and their attendance\n"
                "`/meeting last` — stats for the most recent meeting\n"
                "`/meeting lock` / `/meeting unlock` — manage the lockout\n"
                "\n**Private voice rooms**\n"
                "`/meeting create @member… [name]` — spin up a private VC\n"
                "`/meeting endroom` — end your room early"
            )

    # ── tracked-meeting helpers ─────────────────────────────────────
    async def _sessions(self, meeting_id: str) -> list[dict]:
        """Attendance rows for a meeting; a read failure reads as empty.

        A report that shows "nobody came" because the hub was unreachable is
        worse than one that says it could not read the log, so the caller gets
        an empty list and the command's own warning covers it.
        """
        try:
            return await store.list_meeting_sessions(meeting_id)
        except StoreError as exc:
            LOG.error("Meetings: could not read attendance for %s: %s",
                      meeting_id, exc)
            return []

    async def _absent_entries(self, meeting: dict) -> tuple[list[str], dict[str, str]]:
        """``(ids, {id: display_name})`` for the absentee page.

        Names come from the guild where available and fall back to the name
        recorded on their attendance row, so an absentee who left the server
        still shows as a name rather than a bare id.
        """
        sessions = await self._sessions(meeting["id"])
        uids = meetlib.absentee_ids(meeting, sessions)
        recorded = {str(r.get("discord_user")): r.get("display_name")
                    for r in sessions if r.get("discord_user")}
        guild = self._guild()
        names: dict[str, str] = {}
        for uid in uids:
            member = guild.get_member(int(uid)) if guild else None
            names[uid] = (member.display_name if member
                          else recorded.get(uid) or f"Member {uid}")
        return uids, names

    async def _absentees_embed(self, meeting: dict, view: "MeetingAbsenteesView"):
        """Build the absentee page: embed plus the card grid for page 1."""
        uids, names = await self._absent_entries(meeting)
        view.build_grid(uids, names)
        return meetlib.absentees_embed(meeting, uids, names)

    def _guild(self) -> discord.Guild | None:
        """The first guild the bot is in -- the club server.

        The bot is single-guild in practice, so the first one is the club.
        Returning None rather than raising keeps the report working in a
        multi-guild deployment instead of failing on a lookup.
        """
        guilds = getattr(self.bot, "guilds", None) or []
        return guilds[0] if guilds else None

    async def _issue_yellow_card(self, member_id: int, issuer: discord.abc.User,
                                 title: str = ""):
        """Write a yellow ``warnings`` row; returns ``(issued, error)``.

        Same write as ``/warn`` so the card is a real record on the member's
        profile, not a message that scrolls away. Members who left the server
        still get a row -- the absence is the fact being recorded, and the
        profile it lands on still exists in the hub.

        ``title`` is the meeting's, passed in rather than read off ``self``:
        ``self.meeting`` on the cog is the *private room* registry, a different
        feature that happens to share the attribute name, so reading it here
        would put a HybridGroup into an f-string the first time anyone pressed
        a card button.
        """
        guild = self._guild()
        member = guild.get_member(member_id) if guild else None
        name = member.name if member else str(member_id)
        try:
            await store.increment_member(member_id, "warnings", 1,
                                        bootstrap={"username": name})
        except StoreError as exc:
            LOG.error("Meetings: yellow card for %s failed: %s", member_id, exc)
            return False, f"Couldn't record the yellow card: {exc}"
        if guild is not None:
            try:
                await store.log_moderation(
                    action="yellow_card",
                    target_id=member_id,
                    moderator_id=issuer.id,
                    moderator_name=issuer.display_name,
                    reason=f"Absent from {title or 'a meeting'}")
            except StoreError as exc:
                # The card is recorded; a missing modlog line must not make the
                # issuer think it failed and press the button again.
                LOG.error("Meetings: modlog write for card on %s failed: %s",
                          member_id, exc)
        return True, None

    async def _live_meeting(self) -> dict | None:
        """The running meeting, or None.

        Tolerates a missing ``meetings`` table: the bot degrades to "no meeting
        running" rather than erroring every /meeting command, which is the same
        contract the rest of the store uses for not-yet-provisioned tables.
        """
        try:
            return await store.get_live_meeting()
        except StoreError as exc:
            LOG.error("Meetings: could not read the live meeting: %s", exc)
            return None

    async def _live_meeting_for_channel(self, channel_id: int) -> dict | None:
        """The running meeting in ``channel_id``, or None.

        This is the shape the voice listener needs — it fires for every channel
        in the guild, so it filters in the hub rather than listing meetings.
        """
        try:
            return await store.get_live_meeting_for_channel(channel_id)
        except StoreError as exc:
            LOG.error("Meetings: live lookup for #%s failed: %s", channel_id, exc)
            return None

    async def _discard_meeting(self, meeting_id: str) -> None:
        """Drop a meeting row that failed to start.

        Meeting rows have no delete accessor (the hub keeps them as a record),
        so the row is flipped to ended rather than removed — an aborted start
        then reads as a zero-length meeting instead of lingering as live.
        """
        try:
            await store.end_meeting(meeting_id)
        except StoreError:
            LOG.exception("Meetings: could not discard meeting %s", meeting_id)

    async def _restore_quietly(self, channel: discord.VoiceChannel,
                               snapshot: dict, granted: set[int]) -> list[str]:
        """Restore permissions, downgrading a failure to a returned warning."""
        try:
            return await meetlib.restore_channel(channel, snapshot, granted=granted)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            LOG.error("Meetings: restore of #%s failed: %s", channel.id, exc)
            return [f"the whole channel ({exc.__class__.__name__})"]

    async def _close_session(self, meeting_id: str, discord_id: int) -> bool:
        try:
            return await store.close_meeting_session(meeting_id, discord_id)
        except StoreError as exc:
            LOG.error("Meetings: could not close attendance for %s: %s",
                      discord_id, exc)
            return False

    # ── attendance: voice-state listener ────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Track meeting attendance as people join, leave and rejoin.

        Two things are recorded from a single voice transition: a join opens a
        new ``meeting_sessions`` row and a leave closes the open one. Because
        the log is append-only, a member who drops out and comes back 20 minutes
        later produces two rows with their own joined_at/left_at, which is what
        the CSV and the report need.

        The lookup is per-channel (``get_live_meeting_for_channel``) so this
        only touches the hub when the transition actually involves a channel
        that currently has a meeting running — this listener fires for every
        voice change in the guild, so a global query here would hammer Appwrite.
        """
        old_id = before.channel.id if before.channel is not None else None
        new_id = after.channel.id if after.channel is not None else None
        if old_id == new_id:
            return
        if member.bot:
            # Bots are never attendees (resolve_audience filters them too), so
            # recording one would just pollute the CSV with the club's music
            # and integration bots sitting in the VC.
            return

        # Resolve which side of the transition is the meeting channel. If the
        # member moved between two non-meeting channels there is nothing to do.
        meeting = await self._live_meeting_for_channel(new_id) if new_id else None
        if meeting is not None:
            await self._record_join(meeting, member)
        elif old_id is not None:
            meeting = await self._live_meeting_for_channel(old_id)
            if meeting is not None:
                await self._record_leave(meeting, member)

        # Keep the private-room auto-cleanup working alongside meeting tracking.
        await self._maybe_destroy_room(before, after)

    async def _record_join(self, meeting: dict, member: discord.Member) -> None:
        """Open an attendance row for ``member`` joining ``meeting``'s channel."""
        # Guard against a duplicate row if two voice events race (Discord
        # occasionally emits an extra update). If the member already has an open
        # session, this is not a new visit.
        try:
            open_rows = await store.list_meeting_sessions(meeting["id"], open_only=True)
        except StoreError as exc:
            LOG.error("Meetings: attendance read failed for %s: %s",
                      meeting["id"], exc)
            return
        if any(str(r["discord_user"]) == str(member.id) for r in open_rows):
            return
        # A member locked out is denied connect, so a voice event can't get them
        # in — but a rejoin *while unlocked* after a lock is a legitimate second
        # visit and should be logged as such.
        note = meetlib.scope_note_for(member, meeting["scope"])
        # "Readmitted" is derived from the meeting's ``granted`` list rather than
        # stamped on the row. The voice listener has no idea which admin pressed
        # /meeting unlock — the grant was recorded at unlock time — and the
        # report only needs to know that the join followed a lockout.
        readmitted = str(member.id) in {str(g) for g in meeting["granted"] or []}
        try:
            await store.record_meeting_join(
                meeting["id"], member.id, member.display_name, scope_note=note)
        except StoreError as exc:
            LOG.error("Meetings: could not record join for %s: %s", member.id, exc)
            return
        LOG.info("Meetings: %s joined %s as %s%s", member.display_name,
                 meeting["title"], note, " (readmitted)" if readmitted else "")

    async def _record_leave(self, meeting: dict, member: discord.Member) -> None:
        """Close ``member``'s open attendance row when they leave the channel."""
        try:
            closed = await store.close_meeting_session(meeting["id"], member.id)
        except StoreError as exc:
            LOG.error("Meetings: could not record leave for %s: %s", member.id, exc)
            return
        if closed:
            LOG.info("Meetings: %s left %s", member.display_name, meeting["title"])

    async def _maybe_destroy_room(self, before, after) -> None:
        """Auto-delete a tracked private room once it empties (legacy flow)."""
        affected = set()
        if before.channel is not None:
            affected.add(before.channel.id)
        if after.channel is not None:
            affected.add(after.channel.id)
        for channel_id in affected:
            if channel_id not in self.meetings:
                continue
            channel = self._find_channel(channel_id)
            if channel is None:
                # Deleted out from under us — drop the stale entry.
                self.meetings.pop(channel_id, None)
                await self._save_meetings()
                continue
            try:
                occupants = len(channel.members)
            except (discord.Forbidden, discord.HTTPException):
                continue
            if occupants == 0:
                await self._destroy_meeting(channel_id)

    # ── commands: tracked meetings ───────────────────────────────────
    @meeting.command(
        name="start",
        description=("Start a tracked meeting in a voice channel, restricted to "
                     "an audience of members."))
    @commands.guild_only()
    @require_meeting_admin()
    @commands.bot_has_permissions(manage_channels=True, manage_roles=True,
                                  connect=True, move_members=True)
    async def meeting_start(self, ctx, channel: discord.VoiceChannel, *,
                            audience: str = "bureau",
                            minutes: int | None = None,
                            also: commands.Greedy[discord.Member] = None):
        """Start a meeting and track who attends.

        `audience` is one of:
        **bureau** — the bureau offices (Archon, President, Vice President,
        Manager) plus every Chief/Lead/Head of a unit or cell.
        **cells** — the bureau plus every member of a unit or cell.
        **all** — every robotics member in the server.

        `minutes` is the planned length, recorded on the meeting so the report
        can show planned vs actual. `also` adds members who don't qualify for
        the audience (guests, visiting members).
        """
        scope = audience.strip().lower()
        if scope not in meetlib.SCOPES:
            await ctx.send(
                f"❓ Unknown audience `{audience}`. Pick one of: "
                + ", ".join(f"`{s}`" for s in meetlib.SCOPES) + ".")
            return
        if minutes is not None and not 1 <= minutes <= 24 * 60:
            await ctx.send("❌ `minutes` must be between 1 and 1440.")
            return

        extras = [m for m in (also or []) if not m.bot]
        if len(extras) > config.MEETING_MAX_EXTRA_MEMBERS:
            await ctx.send(
                f"❌ Too many tagged members — max "
                f"{config.MEETING_MAX_EXTRA_MEMBERS} extra members.")
            return

        # A meeting already running would leave its own snapshot orphaned and
        # make "which meeting am I in?" ambiguous for the voice listener.
        existing = await self._live_meeting()
        if existing is not None:
            where = (f"<#{existing['channel_id']}>"
                     if existing["channel_id"] else "a deleted channel")
            await ctx.send(
                f"⏳ A meeting is already running in {where} "
                f"(`{existing['title']}`). End it with `/meeting end` first.")
            return
        # Starting a meeting in the same channel the running one uses would
        # clobber the live snapshot; the check above catches it, but be explicit
        # about the channel too in case the live lookup missed it.
        clash = await self._live_meeting_for_channel(channel.id)
        if clash is not None:
            await ctx.send(
                f"⏳ A meeting is already running in <#{channel.id}>. "
                f"End it with `/meeting end` first.")
            return

        try:
            audience_members = await meetlib.resolve_audience(
                ctx.guild, scope, extra=extras)
        except ValueError as exc:
            await ctx.send(f"❌ {exc}")
            return
        if not audience_members:
            await ctx.send(
                "❌ Nobody matched that audience — check the role names in "
                "`.env` (or tag members with `also:`).")
            return

        async with ctx.typing():
            # Snapshot *before* the first write; /meeting end restores from this.
            snapshot = meetlib.snapshot_overwrites(channel)
            meeting_id = None
            try:
                meeting_id = await store.create_meeting({
                    "title": f"{meetlib.scope_label(scope)} meeting",
                    "channel_id": str(channel.id),
                    "channel_name": channel.name,
                    "scope": scope,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "planned_minutes": int(minutes or 0),
                    "expected": meetlib.audience_ids(audience_members),
                    # Only the members the role resolver did *not* already pick
                    # up are visitors; listing them all would double-count the
                    # audience and make the report's absent list wrong.
                    "visitors": [str(m.id) for m in extras
                                 if m.id not in
                                 {a.id for a in audience_members}],
                    "created_by": ctx.author.id,
                })
                await store.save_meeting_channel_state(meeting_id, snapshot)
                await meetlib.ensure_bot_access(channel)
                await meetlib.apply_meeting_permissions(
                    channel, audience=audience_members, grant_extra=extras)
            except StoreError as exc:
                LOG.error("Meetings: /meeting start failed: %s", exc)
                # Roll the channel back: a half-started meeting with a narrowed
                # channel and no live row is the worst possible state.
                await self._restore_quietly(channel, snapshot, set())
                if meeting_id:
                    await self._discard_meeting(meeting_id)
                await ctx.send(f"⚠️ Couldn't start the meeting: {exc}")
                return
            except discord.HTTPException as exc:
                LOG.error("Meetings: /meeting start permission write failed: %s", exc)
                await self._restore_quietly(channel, snapshot, set())
                if meeting_id:
                    await self._discard_meeting(meeting_id)
                await ctx.send(f"⚠️ Couldn't narrow the channel: {exc}")
                return

        visitors = [m for m in extras
                      if m.id not in {a.id for a in audience_members}]
        lines = [
            f"🔔 **Meeting started** in <#{channel.id}>",
            f"Audience: **{meetlib.scope_label(scope)}** "
            f"({meetlib.SCOPE_SUMMARY[scope]}) — "
            f"{len(audience_members)} member(s).",
        ]
        if minutes:
            lines.append(f"Planned length: **{minutes} min**.")
        if visitors:
            lines.append("Also invited: "
                         + ", ".join(m.mention for m in visitors) + ".")
        lines.append(f"Attendance ID: `{meeting_id}`")
        await ctx.send("\n".join(lines))

    @meeting.command(name="end", aliases=["finish"],
                     description="End the running meeting and restore the channel.")
    @commands.guild_only()
    @require_meeting_admin()
    async def meeting_end(self, ctx):
        """End the running meeting, restore the channel, keep the attendance."""
        meeting = await self._live_meeting()
        if meeting is None:
            await ctx.send("⏳ No meeting is running right now.")
            return
        channel = (self._find_channel(int(meeting["channel_id"]))
                   if meeting["channel_id"] else None)
        ended_at = datetime.now(timezone.utc).isoformat()

        # Read attendance *before* closing the open rows, so the rollup below still
        # sees who was there.
        try:
            sessions = await store.list_meeting_sessions(meeting["id"])
        except StoreError as exc:
            LOG.error("Meetings: could not read attendance for %s: %s",
                      meeting["id"], exc)
            sessions = []
        problems: list[str] = ([] if sessions else
                               ["couldn't read the attendance log"])

        # Close anyone still in the channel so their time is recorded up to now
        # instead of being left as a row that never ended.
        for uid in [s["discord_user"] for s in sessions if s["open"]]:
            await self._close_session(meeting["id"], int(uid))

        if channel is None:
            problems.append("the channel is gone, so permissions couldn't be restored")
        else:
            snapshot = await store.meeting_channel_state(meeting["id"])
            if not snapshot:
                problems.append(
                    "no saved permission snapshot for this meeting, so the "
                    "channel was left as-is")
            else:
                # Every member the bot granted is expected + visitors; targets that
                # already existed are rewritten from the snapshot, and the rest
                # are deleted so no grant outlives the meeting.
                granted = {*(meeting["expected"] or []),
                           *(meeting["visitors"] or [])}
                failures = await self._restore_quietly(
                    channel, snapshot, set(granted))
                problems.extend(f"couldn't restore {who}" for who in failures)

        try:
            await store.end_meeting(meeting["id"], at=ended_at)
        except StoreError as exc:
            LOG.error("Meetings: could not close meeting %s: %s",
                      meeting["id"], exc)
            await ctx.send(f"⚠️ Ended the channel but couldn't save the "
                           f"meeting record: {exc}")
            return

        totals = meetlib.rollup(sessions, ended_at=ended_at,
                             granted=meeting["granted"])
        where = (f"<#{meeting['channel_id']}>" if channel is not None
                 else "a now-deleted channel")
        header = (f"✅ **Meeting ended** — `{meeting['title']}` in {where}\n"
                  f"{len(totals)} member(s) attended.")
        if meeting["planned_minutes"]:
            header += f" (planned {meeting['planned_minutes']} min)"
        if problems:
            header += "\n⚠️ " + "; ".join(problems)
        header += "\nRun `/meeting last` for the full report."
        await ctx.send(header)

    @meeting.command(
        name="lock",
        description=("Lock the running meeting: anyone who has already left "
                     "cannot rejoin until an admin unlocks them."))
    @commands.guild_only()
    @require_meeting_admin()
    async def meeting_lock(self, ctx):
        """Deny rejoin to everyone who has left the meeting channel.

        Only members who entered *and* left are locked out — they have an
        attendance row with a ``left_at``. Anyone still in the channel (the
        current attendees) and anyone who never joined at all are untouched.

        The lock is implemented as a per-member ``connect`` deny (not a new
        role) so it applies to exactly the people who left and evaporates
        cleanly at ``/meeting end`` when their overwrite is deleted.
        """
        meeting = await self._live_meeting()
        if meeting is None:
            await ctx.send("⏳ No meeting is running right now.")
            return
        channel = (self._find_channel(int(meeting["channel_id"]))
                   if meeting["channel_id"] else None)
        if channel is None:
            await ctx.send("❌ The meeting channel is gone; can't lock it.")
            return

        try:
            sessions = await store.list_meeting_sessions(meeting["id"])
        except StoreError as exc:
            LOG.error("Meetings: lock could not read attendance: %s", exc)
            await ctx.send(f"⚠️ Couldn't read the attendance log: {exc}")
            return

        targets = meetlib.lock_targets(sessions, granted=meeting["granted"])
        if not targets:
            anyone_left = any(s["left_at"] for s in sessions)
            hint = (" — nobody has left the meeting yet, so there is nothing "
                    "to lock") if not anyone_left else \
                   " — everyone who left is still in the channel or was readmitted"
            await ctx.send(f"🔓 Nothing to lock{hint}.")
            return
        if len(targets) > config.MEETING_MAX_LOCKED_OUT:
            await ctx.send(
                f"❌ {len(targets)} members have left — that exceeds the "
                f"{config.MEETING_MAX_LOCKED_OUT} lockout cap (Discord allows "
                f"100 overwrites per channel).")
            return

        locked, failed = [], []
        async with ctx.typing():
            for uid in targets:
                member = ctx.guild.get_member(int(uid))
                if member is None:
                    # Left the server; no overwrite to write.
                    continue
                try:
                    await meetlib.deny_member(
                        channel, member, reason="Meeting lockout")
                    locked.append(uid)
                except (discord.NotFound, discord.Forbidden,
                        discord.HTTPException) as exc:
                    failed.append(f"{member.display_name} ({exc.__class__.__name__})")

        if locked:
            try:
                await store.set_meeting_locked(meeting["id"], True)
            except StoreError as exc:
                LOG.error("Meetings: lock flag not saved: %s", exc)
        names = ", ".join(f"<@{uid}>" for uid in locked[:10])
        more = f" (+{len(locked) - 10} more)" if len(locked) > 10 else ""
        msg = (f"🔒 Locked the meeting — {len(locked)} member(s) that left can no "
               f"longer rejoin: {names}{more}.")
        if failed:
            msg += f"\n⚠️ Couldn't lock: {', '.join(failed[:5])}"
        msg += "\nUse `/meeting unlock @member` to let anyone back in."
        await ctx.send(msg)

    @meeting.command(
        name="unlock",
        description="Let a member back into a locked meeting.")
    @commands.guild_only()
    @require_meeting_admin()
    async def meeting_unlock(self, ctx, member: discord.Member):
        """Clear one member's lockout so they can rejoin the running meeting."""
        meeting = await self._live_meeting()
        if meeting is None:
            await ctx.send("⏳ No meeting is running right now.")
            return
        channel = (self._find_channel(int(meeting["channel_id"]))
                   if meeting["channel_id"] else None)
        if channel is None:
            await ctx.send("❌ The meeting channel is gone; can't unlock it.")
            return

        async with ctx.typing():
            try:
                await meetlib.allow_member(
                    channel, member, reason="Meeting re-entry granted by admin")
            except (discord.NotFound, discord.Forbidden,
                    discord.HTTPException) as exc:
                await ctx.send(f"⚠️ Couldn't let {member.display_name} back in: {exc}")
                return
        try:
            await store.grant_meeting_reentry(meeting["id"], member.id)
        except StoreError as exc:
            LOG.error("Meetings: could not record re-entry for %s: %s",
                      member.id, exc)
        await ctx.send(
            f"✅ {member.mention} can rejoin. They're recorded as readmitted "
            f"in the attendance report.")

    @meeting.command(
        name="last",
        description="Attendance stats for the most recent meeting.")
    @commands.guild_only()
    async def meeting_last(self, ctx):
        """Show the last meeting's report: stats, CSV and the absentee list."""
        try:
            meeting = await store.latest_meeting()
        except StoreError as exc:
            LOG.error("Meetings: /meeting last failed: %s", exc)
            await ctx.send(f"⚠️ Couldn't read the meeting log: {exc}")
            return
        if meeting is None:
            await ctx.send("🗓️ No meetings recorded yet. Start one with "
                           "`/meeting start`.")
            return
        view = MeetingStatsView(self, meeting, user=ctx.author)
        embed, view = await view.interaction_setup()
        await ctx.send(embed=embed, view=view)

    @meeting.command(
        name="list",
        description="Browse past meetings and open their attendance reports.")
    @commands.guild_only()
    async def meeting_list(self, ctx):
        """Pick a meeting from the dropdown to see its attendance."""
        try:
            meetings = await store.list_meetings(25)
        except StoreError as exc:
            LOG.error("Meetings: /meeting list failed: %s", exc)
            await ctx.send(f"⚠️ Couldn't read the meeting log: {exc}")
            return
        if not meetings:
            await ctx.send("🗓️ No meetings recorded yet. Start one with "
                           "`/meeting start`.")
            return
        view = MeetingListView(self, meetings, user=ctx.author)
        await ctx.send(embed=meetlib.list_embed(meetings), view=view)

    # ── commands: private voice rooms ────────────────────────────────
    @commands.hybrid_group(
        name="meeting",
        description="Club meetings with attendance tracking, and private voice rooms.")
    @commands.guild_only()
    async def meeting(self, ctx):
        """Parent group — prints usage when invoked without a subcommand."""
        if ctx.invoked_subcommand is None:
            await ctx.send(
                "🔒 **Meetings**\n"
                "`/meeting start` — start a tracked meeting in a voice channel\n"
                "`/meeting end` — end the running meeting\n"
                "`/meeting list` — browse past meetings and their attendance\n"
                "`/meeting last` — stats for the most recent meeting\n"
                "`/meeting lock` / `/meeting unlock` — manage the lockout\n"
                "\n**Private voice rooms**\n"
                "`/meeting create @member… [name]` — spin up a private VC\n"
                "`/meeting endroom` — end your room early"
            )

    @meeting.command(name="create",
                     description="Create a private voice room with you + the tagged members.")
    @commands.guild_only()
    @commands.bot_has_permissions(manage_channels=True, move_members=True)
    async def meeting_create(self, ctx, members: commands.Greedy[discord.Member], *,
                             name: str | None = None):
        guild = ctx.guild
        author = ctx.author

        allowed = [author]
        seen = {author.id}
        for member in members:
            if member.id in seen or member == author or member.bot:
                continue
            seen.add(member.id)
            allowed.append(member)
        if len(allowed) - 1 > MAX_MEMBERS:
            await ctx.send(f"⛔ That's a crowd — max {MAX_MEMBERS} tagged members per room.")
            return

        room_name = (name or "").strip() or f"{author.display_name}'s meeting"
        room_name = room_name[:100]

        # Place the room in the same category as the author's current VC, if any.
        category = None
        if author.voice and author.voice.channel:
            category = author.voice.channel.category
        try:
            channel = await guild.create_voice_channel(
                f"🔒 {room_name}", category=category,
                reason=f"Private meeting room by {author.name}",
            )
        except discord.HTTPException as exc:
            await ctx.send(f"⚠️ Couldn't create the voice channel: {exc}")
            return

        # Lock it down: everyone loses access, the invitees get it back.
        await channel.set_permissions(guild.default_role, view_channel=False, connect=False)
        await channel.set_permissions(
            guild.me, view_channel=True, connect=True, manage_channels=True, move_members=True
        )
        for member in allowed:
            await self._grant_access(channel, member)

        self.meetings[channel.id] = {"owner": author.id, "members": [m.id for m in allowed]}
        await self._save_meetings()

        # Move everyone who is already in voice on this guild into the room.
        moved = []
        for member in allowed:
            if member.voice and member.voice.channel and member.voice.channel != channel:
                try:
                    await member.move_to(channel)
                    moved.append(member.display_name)
                except discord.HTTPException:
                    pass

        mentions = ", ".join(m.mention for m in allowed)
        message = (
            f"🔒 Private room `{channel.name}` ready for {mentions}.\n"
            "Only the people above can join — it deletes itself once empty."
        )
        if moved:
            message += "\nMoved in: " + ", ".join(moved)
        await ctx.send(message)

    @meeting.command(name="endroom", aliases=["closeroom"],
                     description="Delete your private meeting room.")
    @commands.guild_only()
    async def meeting_endroom(self, ctx):
        mine = [cid for cid, meta in self.meetings.items() if meta.get("owner") == ctx.author.id]
        if not mine:
            await ctx.send("🔒 You don't have an active meeting room.")
            return
        for cid in mine:
            channel = self._find_channel(cid)
            if channel is not None:
                try:
                    await channel.delete(reason=f"Meeting ended by {ctx.author.name}")
                except discord.Forbidden:
                    await ctx.send("⛔ I couldn't delete one of your rooms (missing permission).")
                    continue
            self.meetings.pop(cid, None)
        await self._save_meetings()
        await ctx.send("🔒 Meeting room(s) ended and deleted.")


async def setup(bot):
    await bot.add_cog(Meetings(bot))