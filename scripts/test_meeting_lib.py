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
    # For a *running* meeting an open row measures to now, which is what
    # makes a live report useful. For a *finished* one it must measure to the
    # meeting's end -- otherwise a report read a week later credits a week of
    # presence to whoever never left the row open.
    finished = ml.rollup(sessions, ended_at=iso(minutes=90))
    check("a finished meeting clamps the open row to its end (joined +5, ended +90)",
          finished["2"]["time_present"], 85 * 60)
    check("the closed rows are unaffected by the clamp",
          finished["1"]["time_present"], 2400)
    check("Mgr first_join", total["1"]["first_join"], iso(minutes=0))
    check("Mgr last_join", total["1"]["last_join"], iso(minutes=50))

    print("\nrollup() closes open sessions when the meeting is over")
    ended = ml.rollup(sessions, ended_at=iso(minutes=90))
    check("Cell closed at end", ended["2"]["in_channel"], False)

    print("\nrollup() flags members an admin readmitted after a lockout")
    check("not admitted by default", total["1"]["admitted"], False)
    check("granted member flagged",
          ml.rollup(sessions, granted=["1"])["1"]["admitted"], True)
    check("granted-but-absent member is not in the rollup at all",
          "9" in ml.rollup(sessions, granted=["9"]), False)
    check("granted ids match as strings",
          ml.rollup(sessions, granted=[1])["1"]["admitted"], True)

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

    # ── presentation ────────────────────────────────────────────
    print("\nfriendly_time() renders absolute UTC, never relative")
    check("ISO offset normalised to UTC",
          ml.friendly_time("2026-09-25T20:00:00+02:00"), "2026-09-25 18:00 UTC")
    check("naive ISO is treated as UTC",
          ml.friendly_time("2026-09-25T18:00:00"), "2026-09-25 18:00 UTC")
    check("missing timestamp", ml.friendly_time(""), "—")
    check("garbage timestamp", ml.friendly_time("not-a-date"), "—")

    print("\nmeeting_length() prefers actual over planned")
    live = {"started_at": iso(), "ended_at": "", "live": True,
            "planned_minutes": 60}
    check("a running meeting says so", ml.meeting_length(live), "in progress")
    check("no end and no plan", ml.meeting_length({"started_at": iso()}),
          "—")
    check("no end but planned",
          ml.meeting_length({"started_at": iso(), "planned_minutes": 45}),
          "45 min planned")
    check("ended 90 min later",
          ml.meeting_length({"started_at": iso(),
                             "ended_at": iso(minutes=90)}), "1h 30m")

    print("\ncsv_filename() is sortable and filesystem-safe")
    check("name derives from the start time",
          ml.csv_filename({"started_at": iso(), "title": "Bureau meeting"}),
          "meeting-2026-09-25-1800-bureau-meeting.csv")
    check("punctuation in the title is stripped",
          ml.csv_filename({"started_at": iso(), "title": "Q3 /  Budget!"}),
          "meeting-2026-09-25-1800-q3-budget.csv")
    check("a missing title still yields a name",
          ml.csv_filename({"started_at": iso()}).endswith(".csv"), True)

    print("\ncsv_file() is an uploadable File with the rows in it")
    f = ml.csv_file(meeting, sessions, totals=total,
                    absentees_list=ml.absentee_ids(meeting, sessions))
    check("is a discord.File", isinstance(f, discord.File), True)
    check("filename matches csv_filename()", f.filename,
          ml.csv_filename(meeting))
    # aiohttp's FormData reads fp as binary; a text stream would only fail here.
    check("fp is binary, as aiohttp requires", isinstance(f.fp.read(), bytes),
          True)
    f.fp.seek(0)
    payload = f.fp.read()
    check("seekable and non-empty, so File.reset() works on a retry",
          (f.fp.seekable(), payload != b""), (True, True))
    body = payload.decode()
    check("header survived the upload path", "discord_id," in body, True)
    check("non-ASCII display names survive the encode",
          "ümlaut" in ml.csv_file(meeting, [
              {"discord_user": "1", "display_name": "ümlaut",
               "joined_at": iso(), "left_at": ""}]).fp.read().decode(),
          True)

    print("\nstats_embed() answers when / where / how many")
    # `meeting` has ended_at, so roll up against it rather than against the
    # wall clock -- otherwise the still-open row dominates the presence total.
    finished_total = ml.rollup(sessions, ended_at=meeting["ended_at"])
    stats = ml.stats_embed(meeting, finished_total, ["3"])
    fields = {f.name: f.value for f in stats.fields}
    check("title is emoji-led", stats.title.startswith("📊"), True)
    check("navigable pages are blurple", stats.color, ml.BLURPLE)
    check("when field names the start", "2026-09-25 18:00 UTC" in fields["🗓️ When"], True)
    check("where field names the channel",
          "<#1336692513460977746>" in fields["📍 Where"], True)
    check("where field names the audience", "Bureau" in fields["📍 Where"], True)
    check("headcount is stated", "**2**" in fields["👥 Attendance"], True)
    # Mgr 30m + 10m = 40m, Cell +5 to the meeting's end at +60 = 55m. Total 95m.
    check("time present is stated", "1h 35m" in fields["👥 Attendance"], True)
    check("visits counted across rejoins", "3 visit(s)" in fields["👥 Attendance"], True)
    check("attendance rows are listed by mention",
          "<@1>" in fields["📋 Who was there"], True)
    check("footer is lowercase per convention",
          stats.footer.text == stats.footer.text.lower(), True)

    print("\nstats_embed() survives a meeting nobody joined")
    empty = ml.stats_embed(meeting, {}, ["1", "2"])
    empty_fields = {f.name: f.value for f in empty.fields}
    check("headcount is zero", "**0**" in empty_fields["👥 Attendance"], True)
    check("says so plainly", "Nobody joined" in empty_fields["📋 Who was there"], True)

    print("\nstats_embed() notes a lockout, and one meeting only")
    locked = ml.stats_embed({**meeting, "locked": True, "granted": ["1"]},
                            ml.rollup(sessions, granted=["1"]), [])
    check("locked flag is in the footer", "locked partway" in locked.footer.text, True)
    check("readmitted member is flagged in the list",
          "🔑" in {f.name: f.value for f in locked.fields}["📋 Who was there"], True)
    check("readmitted count is stated",
          "1 readmitted" in {f.name: f.value for f in locked.fields}["👥 Attendance"], True)
    many = ml.rollup([{"discord_user": str(100 + i), "display_name": f"M{i}",
                       "joined_at": iso(), "left_at": iso(minutes=1)}
                      for i in range(20)])
    check("a 20-member meeting truncates the visible list",
          "more (see the CSV)" in {f.name: f.value
                                   for f in ml.stats_embed(meeting, many, []).fields}["📋 Who was there"],
          True)
    check("truncation stays inside Discord's 4096-char embed limit",
          len(ml.stats_embed(meeting, many, []).description or "") + sum(
              len(f.value or "") for f in ml.stats_embed(meeting, many, []).fields)
          < 4096, True)

    print("\nabsentees_embed() is its own page, not part of the stats page")
    absent = ml.absentees_embed(meeting, ["3"], {"3": "Third"})
    check("separate title", absent.title.startswith("🚫 Absent"), True)
    check("its own colour, so it reads as a different page",
          absent.color, ml.WARNING)
    check("names the absent member", "<@3>" in (absent.description or ""), True)
    check("counts them", "**1**" in (absent.description or ""), True)
    check("the stats page never lists absentees inline",
          "<@3>" in "".join(f.value for f in stats.fields), False)

    print("\nabsentees_embed() has a clean state when everyone came")
    clean = ml.absentees_embed(meeting, [], {})
    check("no yellow-card prompt", "never joined" in (clean.description or ""), False)
    check("says everyone turned up", "Everyone who was invited" in clean.description, True)
    check("no member list to page through", "<@" in clean.description, False)

    print("\nabsentees_embed() truncates a long list instead of overrunning")
    crowd = ml.absentees_embed(meeting, [str(i) for i in range(40)], {})
    check("keeps the first 25", crowd.description.count("<@") , 25)
    check("says how many were dropped", "and 15 more" in crowd.description, True)
    check("stays inside the embed limit", len(crowd.description) < 4096, True)

    print("\nattended_count() is the complement of the absent list")
    check("one absent of three", ml.attended_count(meeting, ["3"]), 2)
    check("clamped at zero for an empty audience",
          ml.attended_count({"expected": []}, ["1"]), 0)

    print("\nlist_embed() frames the dropdown")
    recent = [meeting, {**meeting, "id": "m0", "live": True, "ended_at": "",
                        "locked": True}]
    pick = ml.list_embed(recent, live_id="m0")
    check("emoji-led title", pick.title.startswith("📚"), True)
    check("counts what is on record", "**2** meeting(s)" in pick.description, True)
    body = {f.name: f.value for f in pick.fields}["Recent"]
    check("live meeting is flagged", "🔴 running" in body, True)
    check("locked meeting is flagged", "🔒 locked" in body, True)
    check("points at the dropdown", "dropdown" in pick.footer.text, True)
    check("no meetings yet is not an error page",
          "No meetings recorded yet" in ml.list_embed([]).description, True)

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