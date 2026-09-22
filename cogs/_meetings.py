"""Helpers for tracked club meetings (not a cog).

The underscore prefix keeps the auto-loader in ``BOT.py`` and
``scripts/smoke_test.py`` from treating this as an extension; ``cogs/meetings``
imports it with ``from cogs import _meetings as meetlib``.

Responsibilities, all pure enough to unit test:

* resolving a ``/meeting start`` audience scope to a list of members,
* snapshotting and restoring a voice channel's permission overwrites,
* rolling the append-only attendance rows up into per-member totals,
* rendering the attendance CSV and the stat/absentee embeds.

Permission model
----------------
A tracked meeting narrows the voice channel to the audience, then hands it back
verbatim. The snapshot is taken from ``channel.overwrites`` *before* any write,
so ``/meeting end`` is a true restore rather than a best-effort approximation
— the pre-meeting state is reconstructed exactly, including overwrites the bot
did not create.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone

import discord

import config
from cogs._perms import meeting_tier
from data.store import _parse_ts

# What an attendee needs: see the channel, join it, be heard, and stream.
# Kept as permission *names* rather than bit values because a
# ``PermissionOverwrite`` is a per-permission ``True``/``False``/``None`` map in
# discord.py 2.7 — the raw ``allow``/``deny`` bit integers it is built from are
# private to the library and are not part of its public surface.
ATTENDEE_PERMS = {"view_channel": True, "connect": True, "speak": True,
                  "stream": True}
# Same, as the kwargs form ``set_permissions`` accepts directly.
ATTENDEE_KWARGS = dict(ATTENDEE_PERMS)

# The /meeting start audiences, in the order they widen. ``custom`` is not a
# role tier at all -- it is whatever the invoker picked from the member
# dropdown -- so it sorts after the real tiers and skips role resolution.
SCOPES = ("bureau", "cells", "all", "custom")
SCOPE_LABELS = {
    "bureau": "Bureau",
    "cells": "Cell members",
    "all": "All members",
    "custom": "Custom",
}
SCOPE_SUMMARY = {
    "bureau": "bureau offices + every unit/cell head",
    "cells": "bureau + every member of a unit/cell",
    "all": "every robotics member in the server",
    "custom": "only the people picked from the dropdown",
}
# Audience tiers from widest to narrowest. A member qualifies for a scope when
# their tier is at least as *narrow* as it (higher index): a cell member belongs
# to a "cell members" meeting and to an "all members" one, but not to a
# bureau-only meeting.
_TIER_ORDER = ("all", "cells", "bureau")
# Sorts last among tiers — used for anyone tagged in who qualifies for nothing.
_TIER_NONE = len(_TIER_ORDER)
# A chosen audience carries no tier, so every one of its members sorts as an
# equal. Zero matches the in-scope group's own sort key.
_TIER_CUSTOM = 0

# Permissions the meeting layer needs on the channel, for a clear error.
REQUIRED_CHANNEL_PERMS = discord.Permissions(
    manage_channels=True, manage_roles=True, connect=True, move_members=True
)


def scope_label(scope: str) -> str:
    return SCOPE_LABELS.get(scope, scope or "—")


# The words the ``/meeting start audience`` dropdown offers, in order. These are
# also the literal values Discord validates against, so the text form takes the
# same words the slash form displays instead of guessing at internal keys.
SCOPE_CHOICES = ("bureau", "cell members", "all members", "custom")

# Everything a person might reasonably type for a scope, folded to its key.
# The display words come first: someone reading the dropdown sees "cell
# members" and then types "cell members", which would otherwise be rejected as
# an unknown audience because the internal key is ``cells``.
_SCOPE_ALIASES = {
    "bureau": "bureau",
    "bureau only": "bureau",
    "offices": "bureau",
    "cell members": "cells",
    "cell member": "cells",
    "cells": "cells",
    "cell": "cells",
    "all members": "all",
    "all member": "all",
    "all": "all",
    "everyone": "all",
    "custom": "custom",
    "chosen": "custom",
    "picked": "custom",
}


def normalise_scope(text: str) -> str | None:
    """Fold whatever the user typed into a scope key, or ``None``.

    Case, surrounding whitespace, underscores and hyphens are all ignored, so
    ``"Cell-Members "`` and ``"cell members"`` both reach the same scope.
    """
    key = " ".join((text or "").strip().lower()
                  .replace("_", " ").replace("-", " ").split())
    return _SCOPE_ALIASES.get(key)


def _member_rank(member: discord.Member, scope: str) -> tuple[int, int, str]:
    """Sort key for an audience list: in-scope members first, then tagged extras.

    Within the in-scope group the narrowest tier comes first (offices, then
    cell members, then unassigned club members), which reads well in the
    confirmation message. Anyone whose own tier is wider than ``scope`` is
    pushed to the end regardless of tier — they're a guest at this meeting, and
    listing them among the bureau reads as though the role resolution is wrong.

    Falls back to display name so the audience — and therefore the permission
    write order — is stable across identical runs.
    """
    name = member.display_name.lower()
    if scope == "custom":
        # Rank by name only. Comparing roles here would be meaningless — the
        # point of a chosen audience is that roles played no part in it — and
        # ``_TIER_ORDER.index(scope)`` would raise outright.
        return (0, _TIER_CUSTOM, name)
    tier = meeting_tier(member)
    index = _TIER_ORDER.index(tier) if tier in _TIER_ORDER else _TIER_NONE
    if tier in _TIER_ORDER and index >= _TIER_ORDER.index(scope):
        return (0, index, name)
    return (1, _TIER_NONE, name)


async def resolve_audience(guild: discord.Guild, scope: str, *,
                           extra: list[discord.Member] | None = None,
                           limit: int | None = None) -> list[discord.Member]:
    """Members of the audience for ``scope``, widest-last, deterministic order.

    ``extra`` members are always included even if their roles don't qualify —
    that is the point of tagging someone into a meeting they aren't otherwise
    part of. They are merged into the resolved ordering rather than appended so
    a tagged cell member doesn't end up below a plain ``New Member``.

    ``custom`` ignores roles entirely and returns ``extra`` and nothing else,
    so a meeting cannot silently widen itself to whoever happens to hold a
    qualifying role.

    The guild member list is read from the cache on purpose. ``guild.members``
    can be several thousand rows on a big server, and iterating it is cheap;
    the alternative (``query_members``) would require the privileged
    ``Guild Members`` intent, which the bot does not need for this.
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown meeting scope {scope!r}")

    ceiling = config.MEETING_MAX_EXTRA_MEMBERS if limit is None else limit
    extras = [m for m in (extra or []) if not m.bot]
    if len(extras) > ceiling:
        raise ValueError(
            f"{len(extras)} tagged members exceeds the {ceiling} limit")

    seen: dict[int, discord.Member] = {}
    if scope == "custom":
        for member in extras:
            seen.setdefault(member.id, member)
        return sorted(seen.values(), key=lambda m: _member_rank(m, scope))

    for member in guild.members:
        if member.bot:
            continue
        # ``meeting_tier`` returns the member's *highest* tier; widen it to the
        # requested scope instead of testing three separate predicates.
        tier = meeting_tier(member)
        if tier is None:
            continue
        if scope == "bureau" and tier != "bureau":
            continue
        if scope == "cells" and tier not in ("bureau", "cells"):
            continue
        seen[member.id] = member

    for member in extras:
        seen.setdefault(member.id, member)

    return sorted(seen.values(), key=lambda m: _member_rank(m, scope))


def audience_ids(audience: list[discord.Member]) -> list[str]:
    return [str(m.id) for m in audience]


def scope_note_for(member: discord.Member, scope: str) -> str:
    """How one attendee qualified, recorded on their attendance row.

    A member whose tier is *wider* than the meeting's scope is recorded as a
    ``visitor``: they were only there because an admin tagged them in. Their own
    tier is still returned in that case when the scope admits it, so the report
    can distinguish "cell member invited to a bureau meeting" from "random
    outsider invited in".
    """
    tier = meeting_tier(member)
    if tier is None or _TIER_ORDER.index(tier) < _TIER_ORDER.index(scope):
        return "visitor"
    return tier


# ── permission snapshot / restore ───────────────────────────────────────
def snapshot_overwrites(channel: discord.abc.GuildChannel) -> dict:
    """The channel's permission overwrites as a JSON-serialisable dict.

    Each entry is ``{type, perms}`` where ``perms`` is a
    ``PermissionOverwrite`` rendered as ``{permission: True/False}`` with unset
    permissions omitted. That is exactly the shape
    ``PermissionOverwrite(**perms)`` accepts, so ``/meeting end`` rebuilds the
    original overwrite rather than approximating it — a channel whose
    ``@everyone`` denied ``connect`` before the meeting gets it denied again.

    ``type`` is ``"role"``/``"member"`` rather than discord.py's enum so the
    snapshot survives an SDK bump that renumbers the enum.
    """
    out: dict[str, dict] = {}
    for target, overwrite in channel.overwrites.items():
        perms = {name: True for name, value in overwrite
                 if value is True} | {name: False for name, value in overwrite
                                      if value is False}
        out[str(target.id)] = {
            "type": "role" if isinstance(target, discord.Role) else "member",
            "perms": perms,
        }
    return out


# Reserved key inside a meeting's ``meeting_side.<id>`` sidecar. The sidecar is
# otherwise a flat ``target-id -> overwrite`` map, so this is the one entry that
# is metadata *about* the snapshot rather than part of it. ``/meeting end`` skips
# it when replaying and reads it to decide between restoring a channel's
# permissions and deleting a channel the bot created.
CHANNEL_CREATED_KEY = "__bot_created__"


def channel_was_created(snapshot: dict) -> bool:
    """True when the sidecar describes a channel the bot created for this meeting.

    Separate from "the snapshot is empty" on purpose: an empty snapshot is also
    what a *missing* sidecar looks like, and treating that as "delete" would
    have ``/meeting end`` remove a channel it was simply unable to record.
    """
    return bool((snapshot or {}).get(CHANNEL_CREATED_KEY))


def meeting_channel_name(scope: str) -> str:
    """Name for a voice channel the bot creates for a meeting.

    Derived from the audience so two meetings running side by side are
    distinguishable at a glance in the channel list.
    """
    return f"📣 {scope_label(scope)} Meeting"


def overwrite_from_snapshot(entry: dict) -> discord.PermissionOverwrite:
    """Rebuild a :class:`PermissionOverwrite` from a snapshot entry."""
    perms = dict(entry.get("perms") or {})
    # Guard against a hand-edited or truncated sidecar carrying a permission
    # this SDK doesn't know: PermissionOverwrite raises ValueError on an
    # unknown name, which would abort the whole restore.
    known = discord.PermissionOverwrite.VALID_NAMES
    return discord.PermissionOverwrite(
        **{k: bool(v) for k, v in perms.items() if k in known})


def restore_plan(snapshot: dict, *, granted: set[int]) -> dict[str, dict]:
    """What to write, per target id, to put ``channel`` back to ``snapshot``.

    The snapshot is authoritative: every id in it is rewritten to exactly the
    permissions it had before the meeting. Ids in ``granted`` but absent from
    the snapshot are overwrites the bot *created* during the meeting, and those
    are deleted outright rather than denied — ``set_permissions(overwrite=None)``
    removes the entry, which is the only way to get back to a channel that never
    had that member in it. Denying them instead would leave a permanent
    ``view_channel=False`` on every former audience member, which is both wrong
    and hard for a human admin to notice and undo.

    ``set`` distinguishes the two cases: ``True`` means write these permissions,
    ``None`` means delete the overwrite.
    """
    plan: dict[str, dict] = {}
    for oid, entry in (snapshot or {}).items():
        # ``CHANNEL_CREATED_KEY`` is snapshot metadata, not an overwrite; it is
        # read by /meeting end to choose between restoring and deleting, and
        # replaying it as a target would raise on a missing "perms".
        if oid == CHANNEL_CREATED_KEY:
            continue
        plan[str(oid)] = {"type": entry.get("type") or "member",
                          "overwrite": entry}
    for mid in granted:
        oid = str(mid)
        if oid not in plan:
            plan[oid] = {"type": "member", "overwrite": None}
    return plan


async def apply_meeting_permissions(channel: discord.VoiceChannel, *,
                                    audience: list[discord.Member],
                                    grant_extra: list[discord.Member] | None = None
                                    ) -> set[str]:
    """Open ``channel`` to the audience for the duration of the meeting.

    Returns the set of target ids actually written, so the caller can pass it
    to the rollback/cleanup path. This used to return nothing while writing
    three kinds of overwrite, so a mid-way failure rolled back only part of what
    had been applied and left the rest on the channel forever.

    Two behaviours changed here, both of them bugs:

    * **@everyone gets ``view_channel`` only, not ``connect``/``speak``/
      ``stream``.** The full attendee set was granted to @everyone, which meant
      ``connect=True`` for the entire server: *anyone* could join any tracked
      meeting, the audience was never actually enforced, and they were logged as
      attendees. Seeing the channel but being unable to join is what the
      original intent described.
    * The bot's own overwrite (``ensure_bot_access``) is now returned by the
      caller for cleanup too. @everyone and the bot are in neither the snapshot
      nor ``expected``, so neither was ever removed at /meeting end — meaning a
      category-denied channel used for a meeting stayed visible and joinable by
      the whole server permanently, and the bot kept manage_channels/manage_roles
      on it.
    """
    written: set[str] = set()
    # Viewable-but-not-joinable for non-audience members: a hidden channel just
    # looks like a bug to the people who were left out.
    await channel.set_permissions(
        channel.guild.default_role, view_channel=True,
        reason="Meeting in progress")
    written.add(str(channel.guild.default_role.id))
    for member in {*(a for a in audience), *(grant_extra or [])}:
        try:
            await channel.set_permissions(member, **ATTENDEE_KWARGS,
                                          reason="Meeting audience")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            continue
        written.add(str(member.id))
    return written


async def ensure_bot_access(channel: discord.VoiceChannel) -> None:
    """Make sure the bot can always undo the meeting.

    ``Manage Roles``/``Manage Channels`` are needed to restore overwrites, so
    the bot is granted them on the channel for the duration. If this silently
    didn't happen, ``/meeting end`` would 403 and leave the channel locked,
    which is the one failure mode worth engineering against.
    """
    await channel.set_permissions(
        channel.guild.me, **ATTENDEE_KWARGS,
        move_members=True, manage_channels=True, manage_roles=True,
        reason="Bot needs this to end the meeting")


async def deny_member(channel: discord.VoiceChannel, member: discord.Member,
                      *, reason: str) -> None:
    """Lock one member out of the meeting channel.

    ``connect`` is denied without denying ``view_channel``: a member who is
    locked out should still be able to see the channel and read why they can't
    join, rather than finding it silently missing.
    """
    await channel.set_permissions(member, connect=False, reason=reason)


async def allow_member(channel: discord.VoiceChannel, member: discord.Member,
                       *, reason: str) -> None:
    """Let one member back into the meeting channel."""
    await channel.set_permissions(member, **ATTENDEE_KWARGS, reason=reason)


async def restore_channel(channel: discord.VoiceChannel, snapshot: dict, *,
                          granted: set[int]) -> list[str]:
    """Re-apply ``snapshot`` to ``channel``; returns human-readable failures.

    Failures are collected rather than raised: one member whose overwrite can't
    be written must not strand the channel half-restored, so the caller reports
    what it couldn't fix and still closes the meeting.
    """
    plan = restore_plan(snapshot, granted=granted)
    failures: list[str] = []
    for oid, action in plan.items():
        target_id = int(oid)
        member = channel.guild.get_member(target_id)
        role = None if member is not None else channel.guild.get_role(target_id)
        if member is None and role is None:
            # Left the server mid-meeting. Discord ignores overwrites for
            # departed members, so the stale entry is harmless and there is
            # nothing to write it to.
            continue
        entry = action.get("overwrite")
        overwrite = overwrite_from_snapshot(entry) if entry else None
        try:
            await channel.set_permissions(
                member if member is not None else role, overwrite=overwrite,
                reason="Meeting ended — restoring permissions")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            failures.append(f"<@{target_id}> ({exc.__class__.__name__})")
    return failures


# ── attendance rollups ──────────────────────────────────────────────────
def _parse(iso: str) -> datetime | None:
    """Parse a hub timestamp into a **tz-aware** datetime, or None.

    Two bugs lived here. It didn't strip the trailing ``Z``, which
    ``datetime.fromisoformat`` only learned to accept in Python 3.11 — on 3.10
    every hub timestamp returned None, ``_dur_seconds`` fell through to 0, and
    the whole attendance CSV was silently zeroed. And it could return a *naive*
    datetime, which then raised TypeError when compared against an aware value.

    Reuse the store's parser rather than keeping a third copy of this.
    """
    return _parse_ts(iso)


def _dur_seconds(start: str, end: str, until: datetime | None = None) -> int:
    """Seconds between two ISO stamps, clamped at ``until``.

    An unclosed session is measured against ``until`` when one is given and
    against *now* only as a last resort. That distinction matters: a report run
    days after a meeting would otherwise credit an attendee with weeks of
    presence, because the row is still ``open`` in the log if nobody closed it.
    """
    a, b = _parse(start), _parse(end or "")
    if a is None:
        return 0
    if b is None:
        b = until or datetime.now(timezone.utc)
    return max(0, int((b - a).total_seconds()))


def end_plan(snapshot: dict, *, channel_exists: bool,
             granted: set[int]) -> dict:
    """What ``/meeting end`` should do with the meeting's channel.

    Three mutually exclusive outcomes, and picking the wrong one is expensive in
    both directions — deleting a channel the club owns, or leaving a permanent
    ``connect`` deny on a channel that will never be cleaned up again — so the
    decision is named and testable rather than a branch buried in the command:

    ``delete``  the bot created this channel for the meeting, so there is no
                prior state to return it to.
    ``restore`` an existing channel: replay the snapshot and delete the grants
                the bot added.
    ``skip``    nothing to do, or nothing safe to do. ``problem`` carries the
                sentence the user is shown; empty when there is nothing to say.
    """
    if not channel_exists:
        return {"action": "skip", "plan": {}, "problem":
                "the channel is gone, so permissions couldn't be restored"}
    if channel_was_created(snapshot):
        return {"action": "delete", "plan": {}, "problem": ""}
    if not snapshot:
        return {"action": "skip", "plan": {}, "problem":
                "no saved permission snapshot for this meeting, so the channel "
                "was left as-is"}
    return {"action": "restore", "plan": restore_plan(snapshot, granted=granted),
            "problem": ""}


def rollup(sessions: list[dict], *, ended_at: str = "",
           granted: list[str] | None = None) -> dict[str, dict]:
    """Attendance rows -> one summary per member.

    ``sessions`` is the append-only join log, so a rejoin is simply a second
    entry for the same member. Totals are ``time_present`` (sum of the
    intervals), ``visits`` (join count) and ``first_join``/``last_join``.

    ``ended_at`` is both the clamp for still-open sessions and the signal to
    stop reporting anyone as ``in_channel``.

    ``admitted`` is set for anyone in ``granted`` (the members an admin let back
    in after a lockout) who actually turned up — that's the flag the report uses
    to show "was let back in" separately from "was here the whole time".
    """
    readmitted = {str(uid) for uid in (granted or [])}
    until = _parse(ended_at or "")
    out: dict[str, dict] = {}
    for row in sessions or []:
        uid = str(row.get("discord_user") or "")
        if not uid:
            continue
        entry = out.setdefault(uid, {
            "discord_user": uid,
            "display_name": row.get("display_name") or "",
            "scope_note": row.get("scope_note") or "",
            "visits": 0,
            "time_present": 0,
            "first_join": row.get("joined_at") or "",
            "last_join": row.get("joined_at") or "",
            "last_leave": "",
            "in_channel": False,
            "admitted": uid in readmitted,
        })
        entry["visits"] += 1
        entry["time_present"] += _dur_seconds(row.get("joined_at") or "",
                                              row.get("left_at") or "",
                                              until=until)
        entry["last_join"] = row.get("joined_at") or entry["last_join"]
        if row.get("left_at"):
            entry["in_channel"] = False
            entry["last_leave"] = row["left_at"]
        else:
            entry["in_channel"] = True
    # Prefer the most recent name: a member may have renamed mid-meeting.
    for uid, entry in out.items():
        names = [r.get("display_name") for r in sessions or []
                 if str(r.get("discord_user")) == uid and r.get("display_name")]
        if names:
            entry["display_name"] = names[-1]
    if ended_at:
        for entry in out.values():
            if entry["in_channel"]:
                entry["in_channel"] = False
                # The row was closed by the meeting ending, not by the member
                # walking out, so stamp it as leaving at that moment. Without
                # this the row has no ``last_leave`` at all and the "left early"
                # split reads it as someone who got up and went home, which is
                # the one thing they definitely did not do.
                entry["last_leave"] = ended_at
    return out


# Grace period for deciding someone "stayed to the end". ``/meeting end`` closes
# the rows of people still in the channel with the store's own ``now()``, which
# is stamped a moment *after* the meeting's ``ended_at``, so an exact comparison
# would report the entire late audience as having left seconds early. Two
# minutes is far below the gap it is papering over and far above the slop
# between two timestamps taken milliseconds apart.
_STAYED_TOLERANCE = timedelta(minutes=2)


def attendance_split(totals: dict[str, dict], meeting: dict,
                     *, absent: list[str] | None = None) -> dict[str, list]:
    """Split a rollup into the three lists a report is expected to show.

    ``stayed``  joined and was still in the channel when it ended (or is in it
                now, while the meeting is live).
    ``left``    joined, then left before the meeting ended — including people
                who left and came back more than once.
    ``absent``  was in the invited audience and never joined at all.

    The three are a genuine partition of the audience, which is why they can be
    shown as three lists that add up: every expected member appears exactly
    once. Entries are sorted by time present, longest first, so a truncated
    list still leads with the members who were there longest.
    """
    ended = _parse(meeting.get("ended_at") or "")
    stayed: list[dict] = []
    left: list[dict] = []
    for entry in totals.values():
        if entry.get("in_channel"):
            stayed.append(entry)
            continue
        leave = _parse(entry.get("last_leave") or "")
        if ended is not None and leave is not None and (
                leave >= ended - _STAYED_TOLERANCE):
            # Closed by /meeting end rather than by the member disconnecting.
            stayed.append(entry)
        else:
            left.append(entry)

    def _rank(entries: list[dict]) -> list[dict]:
        return sorted(entries,
                      key=lambda e: (-e["time_present"], e["display_name"].lower()))

    return {"stayed": _rank(stayed), "left": _rank(left),
            "absent": list(absent or [])}


def absentee_ids(meeting: dict, sessions: list[dict]) -> list[str]:
    """Ids who were expected but never joined, in the meeting's snapshot order.

    Driven by the ``expected`` list stored at ``/meeting start`` rather than by
    re-resolving the scope now. Re-resolving would silently change the answer
    if someone was promoted, demoted or left the club during the meeting, and
    the absent list is meant to describe *this* meeting's audience.

    Anyone the bot let back in mid-meeting counts as present even if their only
    join happened after the lock — otherwise a readmitted member would show up
    as absent on their own report.
    """
    attended = {str(r.get("discord_user") or "") for r in sessions or []}
    readmitted = {str(uid) for uid in (meeting.get("granted") or [])}
    return [uid for uid in (meeting.get("expected") or [])
            if str(uid) not in attended and str(uid) not in readmitted]


def lock_targets(sessions: list[dict], *, granted: list[str] | None = None) -> list[str]:
    """Member ids a ``/meeting lock`` should deny rejoin to.

    The rule is "entered *and* left", so:
      * a member with a closed row and no open row — locked;
      * a member who left but is currently back in — not locked (they are in the
        room right now; denying them would kick the person running the meeting
        out of their own channel);
      * a member who never joined — not locked, since there is nothing to
        prevent and denying them would invent an absence they didn't make;
      * a member an admin already readmitted — not re-locked.

    Order is first-appearance so the lock output is stable.
    """
    still_in = {str(s["discord_user"]) for s in sessions or [] if s["open"]}
    readmitted = {str(uid) for uid in (granted or [])}
    out: list[str] = []
    seen: set[str] = set()
    for row in sessions or []:
        uid = str(row.get("discord_user") or "")
        if not uid or uid in seen or uid in still_in or uid in readmitted:
            continue
        if not row.get("left_at"):
            continue
        seen.add(uid)
        out.append(uid)
    return out


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


# ── presentation ────────────────────────────────────────────────────────
# Embed colours follow the bot's convention: blurple for a page you can
# navigate away from, amber for a warning, grey for an empty result.
BLURPLE = discord.Color.blurple()
# "orange" rather than "amber" or "yellow": the absentee page is a warning, but
# a saturated yellow embed is unreadable with the default light-theme text.
WARNING = discord.Color.orange()
MUTED = discord.Color.greyple()
SUCCESS = discord.Color.green()


def friendly_time(iso: str, *, ended_at: str = "") -> str:
    """``2026-09-25T18:00:00+00:00`` -> ``2026-09-25 18:00 UTC``.

    Deliberately absolute and UTC rather than a relative "2 hours ago": a
    meeting log is read weeks later, and a relative timestamp on an archived
    record is meaningless. The store keeps ISO-8601 with an offset, so the
    rendered value can differ from the raw column by an hour depending on the
    server's timezone setting.
    """
    parsed = _parse(iso or "")
    if parsed is None:
        return "—"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def meeting_length(meeting: dict) -> str:
    """Actual elapsed time of a meeting, or the planned length if it ran on."""
    start, end = _parse(meeting.get("started_at") or ""), _parse(
        meeting.get("ended_at") or "")
    if start is None:
        return "—"
    if end is None:
        if meeting.get("live"):
            return "in progress"
        planned = meeting.get("planned_minutes")
        return f"{planned} min planned" if planned else "—"
    return format_duration(int((end - start).total_seconds()))


# How many names one of the three attendance rows shows before deferring to the
# CSV. Ten is roughly 200 characters, so three rows plus the four fields above
# stay well inside the embed's 4096-char budget even when every row is full.
_ROW_NAMES = 10


def _name_list(entries: list[dict], *, empty: str) -> str:
    """One line per member: mention, time present, and the oddities worth seeing.

    ``🔑`` marks someone an admin had to readmit after a lockout, and ``2×``
    marks a member who left and came back -- neither is visible anywhere else on
    the page, and both change how the number should be read.
    """
    if not entries:
        return empty
    lines = []
    for entry in entries[:_ROW_NAMES]:
        flag = " 🔑" if entry.get("admitted") else ""
        visits = f" · {entry['visits']}×" if entry.get("visits", 1) > 1 else ""
        lines.append(
            f"<@{entry['discord_user']}> — "
            f"{format_duration(entry.get('time_present', 0))}{visits}{flag}")
    return "\n".join(lines)


def _id_list(ids: list[str], *, empty: str) -> str:
    """One line per Discord id — used for absentees, who have no session row.

    Mentions rather than display names: an absentee never joined, so there is no
    recorded name to show, and a cached one could be years out of date.
    """
    if not ids:
        return empty
    return "\n".join(f"<@{uid}>" for uid in ids[:_ROW_NAMES])


def stats_embed(meeting: dict, totals: dict[str, dict],
                absent: list[str]) -> discord.Embed:
    """The meeting's main stats page.

    Carries what the club asks for -- when, where, how many, and who was
    actually there -- as three lists that partition the invited audience:
    stayed to the end, came and left early, and never showed up. The absentees
    *page* (with its yellow cards) is still one button away: a club-wide meeting
    has forty absentees, and the count here is enough to decide whether to go
    and look.
    """
    attended = len(totals)
    expected = len(meeting.get("expected") or [])
    total_seconds = sum(e["time_present"] for e in totals.values())
    reads = sum(e["visits"] for e in totals.values())
    readmitted = sum(1 for e in totals.values() if e["admitted"])

    embed = discord.Embed(
        title=f"📊 {meeting.get('title') or 'Meeting'}",
        color=BLURPLE,
    )
    where = (f"<#{meeting['channel_id']}>" if meeting.get("channel_id")
             else "a now-deleted channel")
    embed.add_field(name="🗓️ When", value=(
        f"Started {friendly_time(meeting.get('started_at'))}\n"
        f"Length **{meeting_length(meeting)}**"
        + (f" · planned {meeting['planned_minutes']} min"
           if meeting.get("planned_minutes") else "")), inline=False)
    embed.add_field(name="📍 Where", value=(
        f"{where}\nAudience **{scope_label(meeting.get('scope') or '')}**"),
        inline=False)

    parts = attendance_split(totals, meeting, absent=absent)
    stayed, left = parts["stayed"], parts["left"]
    summary = (f"**{attended}** of {expected} invited"
               if expected else f"**{attended}** attended")
    embed.add_field(name="👥 Attendance", value=(
        f"{summary} · {format_duration(total_seconds)} of presence · "
        f"{reads} visit(s)"
        + (f"\n{readmitted} readmitted after a lockout" if readmitted else "")),
        inline=False)

    # The three lists the club actually asks for, side by side. Names are
    # truncated per row rather than the list being dropped, because "who was
    # there" is the question this page exists to answer and an invite-only
    # channel makes that the least visible thing in the server.
    live = bool(meeting.get("live"))
    stayed_name = "🟢 In the channel now" if live else "🟢 Stayed to the end"
    left_name = "🚪 Left early" if not live else "🚪 Left (not back in)"
    embed.add_field(
        name=f"{stayed_name} ({len(stayed)})",
        value=_name_list(stayed, empty="_nobody_") +
        (f"\n…{len(stayed) - _ROW_NAMES} more in the csv"
         if len(stayed) > _ROW_NAMES else ""),
        inline=False)
    embed.add_field(
        name=f"{left_name} ({len(left)})",
        value=_name_list(left, empty="_nobody_") +
        (f"\n…{len(left) - _ROW_NAMES} more in the csv"
         if len(left) > _ROW_NAMES else ""),
        inline=False)
    embed.add_field(
        name=f"🚫 Absent ({len(parts['absent'])})",
        value=_id_list(parts["absent"], empty="_nobody_") +
        (f"\n…{len(parts['absent']) - _ROW_NAMES} more — see the absentees page"
         if len(parts["absent"]) > _ROW_NAMES else ""),
        inline=False)

    # Lowercase throughout: the bot's footers read as captions, not sentences.
    footer = "the csv button has every join and leave with exact timestamps"
    if meeting.get("locked"):
        footer = "🔒 this meeting was locked partway through · " + footer
    embed.set_footer(text=footer)
    return embed


def absentees_embed(meeting: dict, absent: list[str],
                    names: dict[str, str]) -> discord.Embed:
    """The absentee page: who was invited and never turned up.

    Kept separate from :func:`stats_embed` on purpose -- a bureau meeting with
    three absentees and an "all members" meeting with forty are very different
    densities of list, and one yellow-card button per absentee only makes sense
    once the reader has decided they care about this page.
    """
    invited = len(meeting.get("expected") or [])
    present = attended_count(meeting, absent)
    embed = discord.Embed(
        title=f"🚫 Absent — {meeting.get('title') or 'Meeting'}",
        color=WARNING,
    )
    if not absent:
        embed.description = (
            f"Everyone who was invited turned up — **{present}** of "
            f"**{invited}** attended "
            f"{friendly_time(meeting.get('started_at'))}.")
        embed.set_footer(text="no yellow cards needed")
        return embed

    lines = [f"<@{uid}>" + (f" — {names[uid]}" if names.get(uid) else "")
             for uid in absent]
    embed.description = (
        f"**{len(absent)}** of **{invited}** invited members never joined "
        f"{friendly_time(meeting.get('started_at'))}.\n\n"
        + "\n".join(lines[:25])
        + (f"\n…and {len(lines) - 25} more" if len(lines) > 25 else ""))
    embed.set_footer(text=(
        "a yellow card records the absence on their profile — "
        "use ◀️ to go back"))
    return embed


def attended_count(meeting: dict, absent: list[str]) -> int:
    """How many of the invited audience actually showed up."""
    return max(0, len(meeting.get("expected") or []) - len(absent))


def list_embed(meetings: list[dict], *, live_id: str | None = None) -> discord.Embed:
    """The ``/meeting list`` picker page.

    The dropdown itself does the selecting, so this is the context around it:
    how many meetings are on record and what the newest one was.
    """
    embed = discord.Embed(title="📚 Meeting history", color=BLURPLE)
    if not meetings:
        embed.description = ("No meetings recorded yet. Start one with "
                             "`/meeting start`.")
        return embed
    newest = meetings[0]
    embed.description = (
        f"**{len(meetings)}** meeting(s) on record. Newest: "
        f"**{newest.get('title') or '—'}** on "
        f"{friendly_time(newest.get('started_at'))}.")
    lines = []
    for m in meetings[:10]:
        flags = []
        if m.get("live"):
            flags.append("🔴 running")
        if m.get("locked"):
            flags.append("🔒 locked")
        lines.append(
            f"`{friendly_time(m.get('started_at'))}` — "
            f"{m.get('title') or '—'} · {meeting_length(m)}"
            + (f" · {' '.join(flags)}" if flags else ""))
    if len(meetings) > 10:
        lines.append(f"…and {len(meetings) - 10} older ones in the dropdown")
    embed.add_field(name="Recent", value="\n".join(lines), inline=False)
    embed.set_footer(text="pick a meeting from the dropdown to see its report")
    return embed


def redacted_stats_embed(meeting: dict, attended: int) -> discord.Embed:
    """The stats page with every per-member detail removed.

    What a non-bureau member is allowed to know about a meeting that happened:
    that it happened, when, where, and roughly how many turned up. Not who came,
    who left early, or who didn't come — that list is a performance record about
    named individuals, and publishing it in a channel anyone can read is how a
    club ends up with a monthly attendance league table nobody agreed to.

    The counts are kept on purpose. "Twelve of fifteen came" leaks less than the
    names do and is what most people actually want to know.
    """
    embed = discord.Embed(
        title=f"📊 {meeting.get('title') or 'Meeting'}", color=MUTED)
    where = (f"<#{meeting['channel_id']}>" if meeting.get("channel_id")
             else "a now-deleted channel")
    embed.add_field(name="🗓️ When", value=(
        f"Started {friendly_time(meeting.get('started_at'))}\n"
        f"Length **{meeting_length(meeting)}**"), inline=False)
    embed.add_field(name="📍 Where", value=(
        f"{where}\nAudience **{scope_label(meeting.get('scope') or '')}**"),
        inline=False)
    expected = len(meeting.get("expected") or [])
    embed.add_field(
        name="👥 Attendance",
        value=(f"**{attended}** of {expected} invited" if expected
               else f"**{attended}** attended")
        + (f", {len(meeting.get('visitors') or [])} guest(s)"
           if meeting.get("visitors") else ""),
        inline=False)
    embed.set_footer(text=(
        "per-member attendance is kept for the bureau — ask them for the report"))
    return embed


def redacted_list_embed(meetings: list[dict]) -> discord.Embed:
    """The meeting history with no drill-down.

    Same history everyone can already infer from the channel, minus the
    attendance figures that sit behind the dropdown. Keeping the history is
    deliberate: "has the club been meeting?" is not sensitive, and a bot that
    says "no" to an ordinary question trains people to reach for someone who
    *can* answer.
    """
    embed = list_embed(meetings)
    embed.colour = MUTED
    embed.set_footer(text=(
        "attendance per meeting is kept for the bureau — "
        "ask them for the report"))
    return embed


def csv_filename(meeting: dict) -> str:
    """A stable, sortable attachment name: ``meeting-2026-09-25-1800-bureau.csv``."""
    stamp = (_parse(meeting.get("started_at") or "")
             or datetime(1970, 1, 1, tzinfo=timezone.utc))
    slug = re.sub(r"[^a-z0-9]+", "-", (meeting.get("title") or "meeting").lower())
    return (f"meeting-{stamp.strftime('%Y-%m-%d-%H%M')}-"
            f"{slug.strip('-') or 'meeting'}.csv")


def csv_file(meeting: dict, sessions: list[dict], *,
             totals: dict[str, dict] | None = None,
             absentees_list: list[str] | None = None) -> discord.File:
    """:func:`attendance_csv` wrapped as an uploadable :class:`discord.File`.

    The text buffer is encoded into a ``BytesIO`` rather than passed through.
    ``discord.File`` hands ``fp`` straight to aiohttp's ``FormData``, which
    reads it as *binary*; a ``StringIO`` returns ``str`` from ``read()`` and the
    upload fails at send time with a ``TypeError`` rather than at construction,
    which is the worst place to find out. ``BytesIO`` is also seekable with a
    known length, so ``File.reset()`` works when a send is retried.
    """
    buf = attendance_csv(meeting, sessions, totals=totals,
                         absentees_list=absentees_list)
    return discord.File(io.BytesIO(buf.getvalue().encode("utf-8")),
                        filename=csv_filename(meeting))


def attendance_csv(meeting: dict, sessions: list[dict], *,
                   totals: dict[str, dict] | None = None,
                   absentees_list: list[str] | None = None) -> io.StringIO:
    """The attendance CSV: one row per join, plus a totals block.

    One row per join rather than one row per member is deliberate — it's the
    same append-only shape the hub stores, so the export is a projection rather
    than a re-derivation, and a member who left and came back shows both visits
    with their own timestamps instead of one averaged row.
    """
    meeting_id = meeting.get("id") or ""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([f"# meeting: {meeting.get('title') or meeting_id}"])
    writer.writerow([f"# channel: #{meeting.get('channel_name') or '—'}",
                     f"({meeting.get('channel_id') or '—'})"])
    writer.writerow([f"# scope: {scope_label(meeting.get('scope') or '')}"])
    writer.writerow([f"# started: {meeting.get('started_at') or '—'}"])
    writer.writerow([f"# ended: {meeting.get('ended_at') or '—'}"])
    if meeting.get("planned_minutes"):
        writer.writerow([f"# planned_minutes: {meeting['planned_minutes']}"])
    writer.writerow([f"# locked: {'yes' if meeting.get('locked') else 'no'}"])
    writer.writerow([])

    writer.writerow(["discord_id", "display_name", "scope", "joined_at",
                     "left_at", "minutes_present"])
    # Same clamp as rollup(): an unclosed row in a finished meeting is measured
    # to the meeting's end, not to whenever the export happens to run.
    until = _parse(meeting.get("ended_at") or "")
    for row in sessions or []:
        writer.writerow([
            row.get("discord_user") or "",
            row.get("display_name") or "",
            row.get("scope_note") or "",
            row.get("joined_at") or "",
            row.get("left_at") or "",
            round(_dur_seconds(row.get("joined_at") or "",
                               row.get("left_at") or "", until=until) / 60, 2),
        ])
    writer.writerow([])

    totals = totals if totals is not None else rollup(sessions)
    writer.writerow(["# totals", "discord_id", "display_name", "visits",
                     "minutes_present", "still_in_channel", "readmitted"])
    for uid in sorted(totals, key=lambda u: totals[u]["display_name"].lower()):
        entry = totals[uid]
        writer.writerow([
            "# totals", uid, entry["display_name"], entry["visits"],
            round(entry["time_present"] / 60, 2),
            "yes" if entry["in_channel"] else "no",
            "yes" if entry["admitted"] else "no",
        ])
    writer.writerow([])

    writer.writerow(["# absent", "discord_id", "display_name"])
    for uid in absentees_list or []:
        writer.writerow(["# absent", uid, ""])
    return buf