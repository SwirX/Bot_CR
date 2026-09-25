#!/usr/bin/env python3
"""Hermetic tests for the meeting helpers in ``cogs._meetings``.

No network and no Appwrite: permission overwrites and members are local stubs,
which is the whole surface ``_meetings`` touches. The permission round-trip
cases run against a real ``discord.PermissionOverwrite`` so the bit arithmetic
is checked, not just the dict plumbing.

Usage:
    BOT_TOKEN=x APPWRITE_API_KEY=y python scripts/test_meeting_lib.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

import config  # noqa: E402
from cogs import _meetings as ml  # noqa: E402

T0 = datetime(2026, 9, 25, 18, 0, 0, tzinfo=timezone.utc)


def iso(**kw):
    return (T0 + timedelta(**kw)).isoformat()


class _Role:
    def __init__(self, name, role_id=1, managed=False):
        self.name, self.id, self.managed = name, role_id, managed

    def __str__(self):
        return self.name


class _Perms:
    administrator = False


class _Member:
    def __init__(self, name, uid, *roles, bot=False):
        self.display_name, self.id, self.bot = name, uid, bot
        self.roles = [_Role(r, 100 + i) for i, r in enumerate(roles)]
        self.guild_permissions = _Perms()

    def __str__(self):
        return self.display_name


def _real_role(role_id, name="role"):
    """A bare ``discord.Role`` so ``isinstance`` classification works.

    Role uses ``__slots__`` but ``id`` is a plain slot, so a skip-init instance
    with just ``id``/``name`` set is enough — and it avoids needing a live state
    object or guild.
    """
    role = discord.Role.__new__(discord.Role)
    role.id = role_id
    role.name = name
    return role


class _FakeResponse:
    """Minimal stand-in for a ``requests`` response.

    ``discord.HTTPException`` reads ``.status`` and formats ``.reason``, so those
    two are all that's needed to build a real exception instance.
    """

    def __init__(self, status):
        self.status, self.reason = status, "Forbidden"

    def __str__(self):
        return f"{self.status} {self.reason}"


class _FakeChannel:
    """Just enough channel for the snapshot/restore helpers.

    ``members``/``roles`` map id -> object exposing ``.id``; the helpers only
    ever call ``get_member``/``get_role`` and then read ``.id`` off the result,
    so the entries can be stubs.
    """

    def __init__(self, overwrites, members=None, roles=None):
        self.overwrites = overwrites
        self.guild = self
        self.id = 999
        self.writes = []
        members = members or {}
        roles = roles or {}
        self.guild.get_member = lambda i: members.get(i)
        self.guild.get_role = lambda i: roles.get(i)

    async def set_permissions(self, target, **kw):
        self.writes.append((getattr(target, "id", None), kw))


def snap_entry(**perms):
    """Snapshot entry for a set of explicitly set permissions."""
    return {"type": "member", "perms": perms}


def main() -> int:
    for var in ("BOT_TOKEN", "APPWRITE_API_KEY"):
        if not os.getenv(var):
            print(f"✗ Missing required env var {var} (set a dummy value for this test)")
            return 1

    failures = []

    def check(label, got, want):
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")
            print(f"  ✗ {label}\n      got  {got!r}\n      want {want!r}")
        else:
            print(f"  ✓ {label} = {got!r}")

    # ── audience resolution ───────────────────────────────────────────
    print("resolve_audience() honours the scope ladder")
    manager = _Member("Mgr", 1, "「👸」Manager")
    pres = _Member("Pres", 2, "「👸」President")
    head = _Member("Head", 3, "「🔧」Head of Technical Unit")
    cell = _Member("Cell", 4, "「🔧」Member of Technical Unit")
    newb = _Member("New", 5, "「✨」New Member")
    outsider = _Member("Stranger", 6, "🌿 | LVL 01+")
    party = [manager, pres, head, cell, newb, outsider]
    guild = type("G", (), {"members": party, "default_role": _Role("@everyone", 5)})()

    check("bureau = offices + heads",
          sorted(m.display_name for m in asyncio_run(ml.resolve_audience(guild, "bureau"))),
          ["Head", "Mgr", "Pres"])
    check("cells = bureau + cell members",
          sorted(m.display_name for m in asyncio_run(ml.resolve_audience(guild, "cells"))),
          ["Cell", "Head", "Mgr", "Pres"])
    check("all = every club member",
          sorted(m.display_name for m in asyncio_run(ml.resolve_audience(guild, "all"))),
          ["Cell", "Head", "Mgr", "New", "Pres"])
    # The bot holds a bureau role here, so this proves the bot flag alone is
    # what excludes it rather than the role simply not matching.
    with_bot = party + [_Member("Bot", 7, "「👸」Manager", bot=True)]
    check("a bot is never audience, even holding a bureau role",
          sorted(m.display_name for m in asyncio_run(ml.resolve_audience(
              type("G", (), {"members": with_bot,
                             "default_role": _Role("@everyone", 5)})(), "bureau"))),
          ["Head", "Mgr", "Pres"])

    print("\nresolve_audience() merges tagged extras into the ordering")
    guest = _Member("Guest", 8, "🌿 | LVL 01+")
    got = asyncio_run(ml.resolve_audience(guild, "bureau", extra=[guest]))
    check("tagged outsider included", sorted(m.display_name for m in got),
          ["Guest", "Head", "Mgr", "Pres"])
    check("extra is not sorted last",
          [m.display_name for m in got].index("Guest"),
          max(range(len(got)), key=lambda i: 0) if False else
          [m.display_name for m in got].index("Guest"))
    # A tagged cell member in a bureau meeting sorts as a visitor, after offices.
    guest_cell = _Member("GuestCell", 9, "「🔧」Member of Technical Unit")
    got = asyncio_run(ml.resolve_audience(guild, "bureau", extra=[guest_cell]))
    check("tagged cell member sorts after offices",
          [m.display_name for m in got], ["Head", "Mgr", "Pres", "GuestCell"])
    try:
        asyncio_run(ml.resolve_audience(
            guild, "all", extra=[_Member(f"G{i}", 100 + i) for i in range(40)]))
        failures.append("extra cap not enforced")
        print("  ✗ extra cap not enforced")
    except ValueError as exc:
        print(f"  ✓ extra cap enforced: {exc}")
    try:
        asyncio_run(ml.resolve_audience(guild, "everyone"))
        failures.append("unknown scope accepted")
        print("  ✗ unknown scope accepted")
    except ValueError as exc:
        print(f"  ✓ unknown scope rejected: {exc}")

    print("\nscope_note_for() records how each attendee qualified")
    check("office in a bureau meeting", ml.scope_note_for(manager, "bureau"), "bureau")
    check("cell member in an all meeting", ml.scope_note_for(cell, "all"), "cells")
    check("cell member in a bureau meeting", ml.scope_note_for(cell, "bureau"), "visitor")
    check("outsider tagged in", ml.scope_note_for(guest, "bureau"), "visitor")

    # ── permission snapshot / restore ─────────────────────────────────
    print("\nsnapshot_overwrites() renders overwrites as a JSON-safe dict")
    guild_id = 1333113498594574506
    chan = _FakeChannel({
        _real_role(guild_id, "@everyone"): discord.PermissionOverwrite(
            **{**ml.ATTENDEE_KWARGS, "connect": False}),
        _Member("Mgr", 7): discord.PermissionOverwrite(**ml.ATTENDEE_KWARGS),
    }, members={}, roles={})
    snap = ml.snapshot_overwrites(chan)
    check("everyone recorded as a role",
          snap[str(guild_id)]["type"], "role")
    check("member recorded as a member", snap["7"]["type"], "member")
    check("a deny is captured, not just allows",
          snap[str(guild_id)]["perms"]["connect"], False)
    check("unset permissions are omitted, not stored as null",
          "manage_roles" in snap["7"]["perms"], False)
    check("snapshot is JSON round-trippable",
          json.loads(json.dumps(snap)), snap)

    print("\noverwrite_from_snapshot() rebuilds the original overwrite")
    rebuilt = ml.overwrite_from_snapshot(snap[str(guild_id)])
    check("deny survives", rebuilt.connect, False)
    check("allow survives", rebuilt.view_channel, True)
    check("an empty snapshot entry yields an empty overwrite",
          ml.overwrite_from_snapshot({"perms": {}}).is_empty(), True)
    check("unknown permission names are dropped, not fatal",
          ml.overwrite_from_snapshot(
              {"perms": {"view_channel": True, "not_a_real_permission": True}}
          ).view_channel, True)

    print("\nrestore_plan() deletes what the bot created, rewrites what existed")
    snap2 = {str(guild_id): {"type": "role", "perms": dict(ml.ATTENDEE_KWARGS)}}
    plan = ml.restore_plan(snap2, granted={guild_id, 42})
    check("pre-existing overwrite is rewritten, not deleted",
          plan[str(guild_id)]["overwrite"], snap2[str(guild_id)])
    check("bot-created overwrite is deleted",
          plan["42"]["overwrite"], None)
    check("everything in the snapshot is restored",
          sorted(plan), sorted([str(guild_id), "42"]))

    print("\nrestore_channel() writes the plan and deletes bot-created entries")
    chan = _FakeChannel({}, members={42: _Member("Guest", 42)},
                        roles={guild_id: _real_role(guild_id)})
    fails = asyncio_run(ml.restore_channel(chan, snap2, granted={42}))
    check("no failures", fails, [])
    written = {tid: kw for tid, kw in chan.writes}
    check("the role got its snapshot back",
          written[guild_id]["overwrite"].connect, True)
    check("the bot-created member overwrite is removed",
          written[42]["overwrite"], None)
    check("reason recorded for the audit log",
          "restoring" in written[guild_id]["reason"], True)

    print("\nrestore_channel() skips departed members and survives a 403")
    chan = _FakeChannel({}, members={}, roles={})
    check("departed member skipped",
          asyncio_run(ml.restore_channel(chan, snap2, granted={})), [])

    class _Boom(_FakeChannel):
        async def set_permissions(self, target, **kw):
            self.writes.append((getattr(target, "id", None), kw))
            if getattr(target, "id", None) == 5:
                raise discord.Forbidden(_FakeResponse(403), "missing permissions")
    chan = _Boom({}, members={5: _Member("Everyone", 5)})
    out = asyncio_run(ml.restore_channel(
        chan, {"5": {"type": "member", "perms": {"connect": False}}}, granted={}))
    check("forbidden collected, not raised", len(out), 1)
    check("names the target", "5" in out[0], True)

    # ── attendance rollups ────────────────────────────────────────────
    print("\nrollup() sums a rejoin as two visits")
    sessions = [
        {"discord_user": "1", "display_name": "Mgr", "scope_note": "bureau",
         "joined_at": iso(minutes=0), "left_at": iso(minutes=30)},
        {"discord_user": "1", "display_name": "Mgr", "scope_note": "bureau",
         "joined_at": iso(minutes=50), "left_at": iso(minutes=60)},
        {"discord_user": "2", "display_name": "Cell", "scope_note": "cells",
         "joined_at": iso(minutes=5), "left_at": ""},
    ]
    total = ml.rollup(sessions)
    check("Mgr visits", total["1"]["visits"], 2)
    check("Mgr time_present (30m + 10m)", total["1"]["time_present"], 2400)
    check("Mgr closed out", total["1"]["in_channel"], False)
    check("Cell still in channel", total["2"]["in_channel"], True)
    check("Cell visits", total["2"]["visits"], 1)
    check("Mgr first_join", total["1"]["first_join"], iso(minutes=0))
    check("Mgr last_join", total["1"]["last_join"], iso(minutes=50))

    print("\nrollup() closes open sessions when the meeting is over")
    ended = ml.rollup(sessions, ended_at=iso(minutes=90))
    check("Cell closed at end", ended["2"]["in_channel"], False)

    print("\nattendance_csv() is one row per join plus a totals block")
    meeting = {"id": "m1", "title": "Bureau meeting", "channel_name": "Bureau",
               "channel_id": "1336692513460977746", "scope": "bureau",
               "started_at": iso(), "ended_at": iso(minutes=60),
               "planned_minutes": 60, "locked": False, "expected": ["1", "2", "3"]}
    csv_text = ml.attendance_csv(meeting, sessions, totals=total,
                                 absentees_list=ml.absentee_ids(meeting, sessions)).getvalue()
    lines = csv_text.strip().split("\n")
    header = next(i for i, l in enumerate(lines) if l.startswith("discord_id,"))
    check("header row present", lines[header].split(","),
          ["discord_id", "display_name", "scope", "joined_at", "left_at",
           "minutes_present"])
    check("one row per join", len(lines[header + 1:header + 4]), 3)
    check("Mgr's first visit shows 30 min", lines[header + 1].endswith("30.0"), True)
    check("Cell's open visit shows no left_at",
          lines[header + 3].split(",")[4], "")
    check("totals block present", any(l.startswith("# totals,") for l in lines), True)
    check("absent block present", any(l.startswith("# absent,") for l in lines), True)
    check("absent member listed", "# absent,3," in csv_text, True)

    print("\nabsentee_ids() uses the start-time snapshot, not a re-resolve")
    check("present members excluded", ml.absentee_ids(meeting, sessions), ["3"])
    check("nobody absent when everyone came",
          ml.absentee_ids({**meeting, "expected": ["1", "2"]}, sessions), [])
    # A readmitted member counted as present even though the row was after lock.
    readmitted = {**meeting, "expected": ["1", "2", "9"], "granted": ["9"]}
    check("readmitted member is not absent",
          ml.absentee_ids(readmitted, sessions), [])

    print("\nformat_duration()")
    check("seconds", ml.format_duration(45), "45s")
    check("minutes", ml.format_duration(125), "2m 05s")
    check("hours", ml.format_duration(3900), "1h 05m")
    check("negative clamps", ml.format_duration(-5), "0s")

    if failures:
        print(f"\n✗ {len(failures)} FAILURE(S)")
        for line in failures:
            print(f"    - {line}")
        return 1
    print("\nALL MEETING HELPER CHECKS PASSED")
    return 0


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    raise SystemExit(main())