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
from datetime import datetime, timezone

import discord

import config
from cogs._perms import meeting_tier

# What an attendee needs: see the channel, join it, be heard, and stream.
# Kept as permission *names* rather than bit values because a
# ``PermissionOverwrite`` is a per-permission ``True``/``False``/``None`` map in
# discord.py 2.7 — the raw ``allow``/``deny`` bit integers it is built from are
# private to the library and are not part of its public surface.
ATTENDEE_PERMS = {"view_channel": True, "connect": True, "speak": True,
                  "stream": True}
# Same, as the kwargs form ``set_permissions`` accepts directly.
ATTENDEE_KWARGS = dict(ATTENDEE_PERMS)

# The three /meeting start audiences, in the order they widen.
SCOPES = ("bureau", "cells", "all")
SCOPE_LABELS = {
    "bureau": "Bureau",
    "cells": "Cell members",
    "all": "All members",
}
SCOPE_SUMMARY = {
    "bureau": "bureau offices + every unit/cell head",
    "cells": "bureau + every member of a unit/cell",
    "all": "every robotics member in the server",
}
# Audience tiers from widest to narrowest. A member qualifies for a scope when
# their tier is at least as *narrow* as it (higher index): a cell member belongs
# to a "cell members" meeting and to an "all members" one, but not to a
# bureau-only meeting.
_TIER_ORDER = ("all", "cells", "bureau")
# Sorts last among tiers — used for anyone tagged in who qualifies for nothing.
_TIER_NONE = len(_TIER_ORDER)

# Permissions the meeting layer needs on the channel, for a clear error.
REQUIRED_CHANNEL_PERMS = discord.Permissions(
    manage_channels=True, manage_roles=True, connect=True, move_members=True
)


def scope_label(scope: str) -> str:
    return SCOPE_LABELS.get(scope, scope or "—")


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
    tier = meeting_tier(member)
    index = _TIER_ORDER.index(tier) if tier in _TIER_ORDER else _TIER_NONE
    if tier in _TIER_ORDER and index >= _TIER_ORDER.index(scope):
        return (0, index, member.display_name.lower())
    return (1, _TIER_NONE, member.display_name.lower())


async def resolve_audience(guild: discord.Guild, scope: str, *,
                           extra: list[discord.Member] | None = None,
                           limit: int | None = None) -> list[discord.Member]:
    """Members of the audience for ``scope``, widest-last, deterministic order.

    ``extra`` members are always included even if their roles don't qualify —
    that is the point of tagging someone into a meeting they aren't otherwise
    part of. They are merged into the resolved ordering rather than appended so
    a tagged cell member doesn't end up below a plain ``New Member``.

    The guild member list is read from the cache on purpose. ``guild.members``
    can be several thousand rows on a big server, and iterating it is cheap;
    the alternative (``query_members``) would require the privileged
    ``Guild Members`` intent, which the bot does not need for this.
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown meeting scope {scope!r}")

    ceiling = config.MEETING_MAX_EXTRA_MEMBERS if limit is None else limit
    seen: dict[int, discord.Member] = {}
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

    extras = [m for m in (extra or []) if not m.bot]
    if len(extras) > ceiling:
        raise ValueError(
            f"{len(extras)} tagged members exceeds the {ceiling} limit")
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
        plan[str(oid)] = {"type": entry.get("type") or "member",
                          "overwrite": entry}
    for mid in granted:
        oid = str(mid)
        if oid not in plan:
            plan[oid] = {"type": "member", "overwrite": None}
    return plan


async def apply_meeting_permissions(channel: discord.VoiceChannel, *,
                                    audience: list[discord.Member],
                                    grant_extra: list[discord.Member] | None = None) -> None:
    """Open ``channel`` to the audience for the duration of the meeting.

    ``@everyone`` is granted the attendee permissions so non-audience members
    can still *see* the channel and get a clear "you weren't invited" from
    Discord rather than a channel that simply doesn't exist in their sidebar —
    a hidden channel looks like a bug to the people who were left out.
    """
    await channel.set_permissions(
        channel.guild.default_role, **ATTENDEE_KWARGS,
        reason="Meeting in progress")
    for member in {*(a for a in audience), *(grant_extra or [])}:
        await channel.set_permissions(member, **ATTENDEE_KWARGS,
                                      reason="Meeting audience")


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
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _dur_seconds(start: str, end: str) -> int:
    a, b = _parse(start), _parse(end or "")
    if a is None:
        return 0
    return max(0, int(((b or datetime.now(timezone.utc)) - a).total_seconds()))


def rollup(sessions: list[dict], *, ended_at: str = "") -> dict[str, dict]:
    """Attendance rows -> one summary per member.

    ``sessions`` is the append-only join log, so a rejoin is simply a second
    entry for the same member. Totals are ``time_present`` (sum of the
    intervals), ``visits`` (join count) and ``first_join``/``last_join``.
    """
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
            "in_channel": False,
            "admitted": False,
        })
        entry["visits"] += 1
        entry["time_present"] += _dur_seconds(row.get("joined_at") or "",
                                              row.get("left_at") or "")
        entry["last_join"] = row.get("joined_at") or entry["last_join"]
        if row.get("left_at"):
            entry["in_channel"] = False
        else:
            entry["in_channel"] = True
        if row.get("admitted_by"):
            entry["admitted"] = True
    # Prefer the longest-joined session's name; a member may have renamed.
    for uid, entry in out.items():
        names = [r.get("display_name") for r in sessions or []
                 if str(r.get("discord_user")) == uid and r.get("display_name")]
        if names:
            entry["display_name"] = names[-1]
    if ended_at:
        for entry in out.values():
            if entry["in_channel"]:
                entry["in_channel"] = False
    return out


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


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


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
    for row in sessions or []:
        writer.writerow([
            row.get("discord_user") or "",
            row.get("display_name") or "",
            row.get("scope_note") or "",
            row.get("joined_at") or "",
            row.get("left_at") or "",
            round(_dur_seconds(row.get("joined_at") or "",
                               row.get("left_at") or "") / 60, 2),
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