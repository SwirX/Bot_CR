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
from cogs._perms import require_meeting_admin
from data.store import store
from data.store import StoreError

LOG = logging.getLogger("bot.meetings")

SETTINGS_KEY = "meetings"
MAX_MEMBERS = 15  # sane cap on tagged members per private room


class Meetings(commands.Cog):
    """Private voice rooms that clean themselves up."""

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

    # ── listeners ────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Delete tracked rooms the moment they empty out."""
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

        totals = meetlib.rollup(sessions, ended_at=ended_at)
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