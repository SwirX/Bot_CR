#!/usr/bin/env python3
"""Hermetic tests for the tracked-meeting cog: attendance + lockout.

``cogs.meetings`` is driven against fake channels/members and a fake store, so
these cases cover the join/leave/rejoin state machine, the duplicate-join
guard, the "nobody left yet" lock edge case and the lockout cap — the paths
that are impossible to reach with a real voice channel and are therefore most
likely to rot silently.

The store is stubbed rather than the network, so this test asserts *what the
cog asks the store to do*, while ``scripts/test_meeting_store.py`` asserts the
store honours those calls against the real hub. Between them the contract is
covered without needing a live Discord gateway.

Usage:
    BOT_TOKEN=x APPWRITE_API_KEY=y python scripts/test_meeting_cog.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from cogs import _meetings as ml  # noqa: E402
import cogs.meetings as meetings_mod  # noqa: E402
from cogs.meetings import Meetings  # noqa: E402

import config  # noqa: E402

CHANNEL = 1336692513460977746
T0 = datetime(2026, 9, 25, 18, 0, 0, tzinfo=timezone.utc)


def iso(**kw):
    return (T0 + timedelta(**kw)).isoformat()


# ── fakes ──────────────────────────────────────────────────────────────
class _Role:
    def __init__(self, name, role_id=1, managed=False):
        self.name, self.id, self.managed = name, role_id, managed


class _Perms:
    administrator = False


class _Member:
    def __init__(self, name, uid, *roles, bot=False):
        self.display_name, self.id, self.bot = name, uid, bot
        self.roles = [_Role(r, 100 + i) for i, r in enumerate(roles)]
        self.guild_permissions = _Perms()

    @property
    def mention(self):
        return f"<@{self.id}>"


class _Channel:
    """Voice channel stub that records the permission writes it receives."""

    def __init__(self, channel_id=CHANNEL, members=()):
        self.id, self.name, self.members = channel_id, "Bureau", list(members)
        self.guild = _GuildStub()
        self.writes = []

    async def set_permissions(self, target, **kw):
        self.writes.append((getattr(target, "id", None), kw))


class _GuildStub:
    def __init__(self):
        self._members = {}

    def add(self, member):
        self._members[member.id] = member

    def get_member(self, uid):
        return self._members.get(uid)

    def get_role(self, uid):
        return None


class _FakeStore:
    """In-memory stand-in for the meeting slice of ``data.store``."""

    def __init__(self):
        self.sessions: dict[str, list[dict]] = {}
        self.live_by_channel: dict[int, dict] = {}
        self.closed: list[tuple[str, int]] = []
        self.joins: list[tuple[str, int, str]] = []
        self.grants: list[tuple[str, int]] = []
        self.locks: list[tuple[str, bool]] = []
        self.ended: list[str] = []
        self.fail_next = None
        self._clock = 0

    def _next(self):
        self._clock += 1
        return iso(minutes=self._clock)

    def meeting(self, *, scope="bureau", live=True, granted=None, expected=None):
        return {
            "id": "m1", "title": "Bureau meeting", "channel_id": str(CHANNEL),
            "channel_name": "Bureau", "scope": scope,
            "started_at": iso(), "planned_minutes": 60, "ended_at": "",
            "live": live, "locked": False, "expected": expected or [],
            "visitors": [], "granted": granted or [], "created_by": None,
        }

    async def get_live_meeting_for_channel(self, channel_id):
        return self.live_by_channel.get(channel_id)

    async def get_live_meeting(self):
        return next(iter(self.live_by_channel.values()), None)

    async def list_meeting_sessions(self, meeting_id, *, open_only=False):
        rows = self.sessions.get(meeting_id, [])
        return [r for r in rows if r["open"]] if open_only else list(rows)

    async def record_meeting_join(self, meeting_id, discord_id, display_name="",
                                  *, scope_note="", admitted_by=None):
        if self.fail_next == "join":
            raise meetings_mod.StoreError("join exploded")
        self.joins.append((meeting_id, discord_id, scope_note))
        row = {"id": f"s{len(self.sessions.get(meeting_id, []))}", "meeting": meeting_id,
               "discord_user": str(discord_id), "display_name": display_name,
               "scope_note": scope_note, "joined_at": self._next(), "left_at": "",
               "open": True}
        self.sessions.setdefault(meeting_id, []).append(row)
        return row["id"]

    async def close_meeting_session(self, meeting_id, discord_id, *, at=None):
        for row in self.sessions.get(meeting_id, []):
            if row["discord_user"] == str(discord_id) and row["open"]:
                row["open"] = False
                row["left_at"] = self._next()
                self.closed.append((meeting_id, discord_id))
                return True
        return False

    async def set_meeting_locked(self, meeting_id, locked):
        self.locks.append((meeting_id, locked))

    async def grant_meeting_reentry(self, meeting_id, discord_id):
        self.grants.append((meeting_id, discord_id))

    async def end_meeting(self, meeting_id, *, at=None):
        self.ended.append(meeting_id)
        for cid in [c for c, m in self.live_by_channel.items()
                    if m["id"] == meeting_id]:
            del self.live_by_channel[cid]


def _voice(after_id, before_id=None):
    """``(before, after)`` channel-state pair for the listener.

    A tiny object with just ``.channel`` is enough — the listener only reads
    ``before.channel``/``after.channel`` — and avoids constructing a real
    ``discord.VoiceState`` (which needs a live client and has moved signatures
    between SDK versions).
    """
    class _State:
        def __init__(self, cid):
            self.channel = _Channel(cid) if cid else None
    return _State(before_id), _State(after_id)


def build_cog(store, members):
    cog = Meetings.__new__(Meetings)
    cog.bot = type("B", (), {"guilds": []})()
    cog.meetings = {}
    channel = _Channel()
    for m in members:
        channel.guild.add(m)
    cog.bot.guilds = [channel.guild]
    cog._test_channel = channel
    # Point the cog at the fake store for the duration of the test.
    cog._store = store
    return cog


def drive(cog, store, member, before_id, after_id):
    """Run the listener once, with the fake store patched in.

    The cog reaches the hub through the module-level ``store`` import, so the
    patch here is what lets the state machine run without a network.
    """
    async def _go():
        real = meetings_mod.store
        meetings_mod.store = store
        try:
            await cog.on_voice_state_update(member, *_voice(after_id, before_id))
        finally:
            meetings_mod.store = real
    return asyncio.run(_go())


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

    mgr = _Member("Mgr", 1, "「👸」Manager")
    cell = _Member("Cell", 2, "「🔧」Member of Technical Unit")
    other = _Member("Other", 3, "「✨」New Member")
    botu = _Member("Music", 4, "「👸」Manager", bot=True)

    # ── attendance state machine ──────────────────────────────────────
    print("join → leave → rejoin produces two attendance rows")
    store = _FakeStore()
    store.live_by_channel[CHANNEL] = store.meeting(scope="all")
    cog = build_cog(store, [mgr, cell, other, botu])

    drive(cog, store, mgr, None, CHANNEL)
    check("first join recorded", len(store.joins), 1)
    check("scope note from the meeting scope",
          store.joins[0][2], "bureau")

    drive(cog, store, mgr, CHANNEL, None)
    check("leave closed the row", len(store.closed), 1)
    check("no open rows remain", store.sessions["m1"][0]["open"], False)

    drive(cog, store, mgr, None, CHANNEL)
    check("rejoin is a second row", len(store.joins), 2)
    check("two rows for the same member",
          sum(1 for s in store.sessions["m1"]
              if s["discord_user"] == "1"), 2)

    print("\na duplicate voice event does not double-count")
    open_rows = [s for s in store.sessions["m1"] if s["open"]]
    check("exactly one open row", len(open_rows), 1)
    drive(cog, store, mgr, None, CHANNEL)
    check("no extra join recorded", len(store.joins), 2)

    print("\nbots and non-meeting channels are ignored")
    before_n = len(store.joins)
    drive(cog, store, botu, None, CHANNEL)
    check("a bot joining is not recorded", len(store.joins), before_n)
    drive(cog, store, other, None, 999)
    check("a join in an unrelated channel is ignored", len(store.joins), before_n)
    drive(cog, store, other, 999, None)
    check("a leave in an unrelated channel is ignored",
          len(store.closed), 1)

    print("\nleaving a non-meeting channel while a meeting runs elsewhere")
    store.live_by_channel.pop(CHANNEL)
    before_c = len(store.closed)
    drive(cog, store, other, 999, None)
    check("nothing recorded", len(store.closed), before_c)

    print("\na store failure while recording a join is logged, not raised")
    store.live_by_channel[CHANNEL] = store.meeting(scope="all")
    store.fail_next = "join"
    drive(cog, store, other, None, CHANNEL)
    check("no join recorded", len(store.joins), before_n)
    check("cog survived the store error", True, True)

    # ── lockout target selection ──────────────────────────────────────
    print("\nlock_targets() picks members who left and are not back in")
    sessions = [
        {"discord_user": "1", "left_at": iso(minutes=10), "open": False},
        {"discord_user": "2", "left_at": iso(minutes=5), "open": False},
        {"discord_user": "2", "left_at": "", "open": True},
        {"discord_user": "3", "left_at": "", "open": False},
    ]
    check("left-and-gone member is locked", ml.lock_targets(sessions), ["1"])
    check("returned member is not locked", "2" in ml.lock_targets(sessions), False)
    check("never-joined member is not locked", "3" in ml.lock_targets(sessions), False)
    check("already-readmitted member is not re-locked",
          ml.lock_targets(sessions, granted=["1"]), [])
    check("empty log locks nobody", ml.lock_targets([]), [])
    check("still-in-only locks nobody",
          ml.lock_targets([{"discord_user": "1", "left_at": "", "open": True}]), [])
    check("a member with two closed visits is listed once",
          ml.lock_targets([{"discord_user": "1", "left_at": iso(), "open": False},
                           {"discord_user": "1", "left_at": iso(), "open": False}]),
          ["1"])
    check("rows with no member id are skipped",
          ml.lock_targets([{"discord_user": "", "left_at": iso(), "open": False}]), [])

    print("\nthe lockout cap is a hard stop, not a silent truncation")
    many = [{"discord_user": str(100 + i), "left_at": iso(), "open": False}
            for i in range(config.MEETING_MAX_LOCKED_OUT + 5)]
    check("more leavers than the cap", len(ml.lock_targets(many)),
          config.MEETING_MAX_LOCKED_OUT + 5)
    check("cap is well under Discord's 100-overwrite limit",
          config.MEETING_MAX_LOCKED_OUT < 100, True)

    print("\ndeny_member() denies connect but keeps the channel visible")
    chan = _Channel()
    asyncio.run(ml.deny_member(chan, mgr, reason="Meeting lockout"))
    tid, kw = chan.writes[0]
    check("targeted the leaver", tid, 1)
    check("connect denied", kw.get("connect"), False)
    check("view left alone so they can see why", "view_channel" in kw, False)

    print("\nallow_member() restores the full attendee permission set")
    chan = _Channel()
    asyncio.run(ml.allow_member(chan, mgr, reason="readmitted"))
    _, kw = chan.writes[0]
    check("attendee perms granted",
          all(kw.get(k) is True for k in ml.ATTENDEE_PERMS), True)

    if failures:
        print(f"\n✗ {len(failures)} FAILURE(S)")
        for line in failures:
            print(f"    - {line}")
        return 1
    print("\nALL MEETING COG CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())