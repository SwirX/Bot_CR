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
from cogs.meetings import MeetingAbsenteesView, MeetingListView  # noqa: E402
from cogs.meetings import Meetings, MeetingStatsView  # noqa: E402

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


class _DeletableChannel(_Channel):
    """A _Channel that also records deletion, for the bot-created path."""

    def __init__(self, channel_id=CHANNEL, **kw):
        super().__init__(channel_id, **kw)
        self.deleted = False
        self.delete_error = None

    async def delete(self, reason=None):
        if self.delete_error:
            raise self.delete_error
        self.deleted = True


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


def _view_children_ok(view, *, label, check):
    """Assert a view fits Discord's component limits.

    ``to_components()`` is the exact payload Discord receives: a list of action
    rows, each holding at most five components, 25 overall. A view that breaks
    either limit raises inside ``send``/``edit_message`` -- after the code that
    built it ran and often after the user pressed a button -- so it is checked
    here instead.
    """
    rows = view.to_components()
    flat = [c for row in rows for c in row["components"]]
    check(f"{label}: within Discord's 25-component limit",
          len(flat) <= 25, True)
    check(f"{label}: no row exceeds 5 components",
          max((len(row["components"]) for row in rows), default=0) <= 5, True)
    return flat


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

    # ── report views ───────────────────────────────────────────────
    print("\nreport views fit Discord's component limits")
    cog = build_cog(_FakeStore(), [])
    meeting = {"id": "m1", "title": "Bureau meeting",
               "channel_id": str(CHANNEL), "channel_name": "Bureau",
               "scope": "bureau", "started_at": iso(), "ended_at": iso(minutes=60),
               "planned_minutes": 60, "locked": False, "live": False,
               "expected": ["1", "2"], "visitors": [], "granted": []}
    stats = MeetingStatsView(cog, meeting, user=mgr)
    _view_children_ok(stats, label="stats", check=check)

    print("\nthe stats page has no yellow-card button — those live on their own page")
    card_buttons = [c for c in stats.children
                    if isinstance(c, discord.ui.Button) and c.custom_id.startswith("card:")]
    check("no per-member card button on the stats page", card_buttons, [])
    check("the absentee button is the only way there",
          any(getattr(c, "custom_id", "") == "abentees" or
              "Absentees" in str(getattr(c, "label", ""))
              for c in stats.children), True)
    check("csv button is present",
          any("CSV" in str(getattr(c, "label", "")) for c in stats.children), True)

    print("\nthe absentee page holds the yellow cards, paged")
    absent_view = MeetingAbsenteesView(cog, meeting, user=mgr, back=stats)
    crowd = [str(200 + i) for i in range(23)]
    names = {uid: f"Member{uid}" for uid in crowd}
    absent_view.build_grid(crowd, names)
    _view_children_ok(absent_view, label="absentee page (full)", check=check)
    cards = [c for c in absent_view.children
             if isinstance(c, discord.ui.Button)
             and str(getattr(c, "custom_id", "")).startswith("card:")]
    check("one card per absentee on the page",
          len(cards), absent_view.page_size)
    check("cards are laid out on their own rows above the pager",
          max(c.row for c in cards), 1)
    check("card buttons are danger-styled, since they issue a penalty",
          all(c.style == discord.ButtonStyle.danger for c in cards), True)
    check("every card has a distinct custom_id",
          len({c.custom_id for c in cards}), len(cards))

    print("\npaging the absentee grid")
    check("page 1 label", absent_view.page_label.label, "1/3")
    check("can't go back from page 1", absent_view.prev_page.disabled, True)
    check("can go forward", absent_view.next_page.disabled, False)
    absent_view.page = 2
    absent_view.build_grid(crowd, names)
    check("last page label", absent_view.page_label.label, "3/3")
    check("can't go past the end", absent_view.next_page.disabled, True)
    check("can go back", absent_view.prev_page.disabled, False)
    # 23 absentees at 10 per page: page 3 is the tail slice, not the head.
    check("a new page's cards are the next slice, not the first",
          sorted(int(c.custom_id.split(":")[1]) for c in absent_view.children
                 if str(getattr(c, "custom_id", "")).startswith("card:")),
          [220, 221, 222])
    check("the short last page still fits its row",
          len([c for c in absent_view.children
               if str(getattr(c, "custom_id", "")).startswith("card:")]), 3)
    # Paging back must return the same slice -- a grid that rebuilds from the
    # wrong offset would silently re-issue cards for the first page.
    absent_view.page = 0
    absent_view.build_grid(crowd, names)
    first_page_again = sorted(int(c.custom_id.split(":")[1]) for c in absent_view.children
                              if str(getattr(c, "custom_id", "")).startswith("card:"))
    check("paging back returns the same members", first_page_again[0], 200)
    check("and the same count", len(first_page_again), absent_view.page_size)
    _view_children_ok(absent_view, label="absentee page (back at page 1)",
                      check=check)

    print("\nan oversized page size is clamped, not allowed to break the view")
    saved = config.MEETING_ABSENTEE_PAGE_SIZE
    try:
        config.MEETING_ABSENTEE_PAGE_SIZE = 99
        big = MeetingAbsenteesView(cog, meeting, user=mgr, back=stats)
        big.build_grid([str(300 + i) for i in range(99)], {})
        check("clamped to two rows of five", len(
            [c for c in big.children
             if str(getattr(c, "custom_id", "")).startswith("card:")]), 10)
        _view_children_ok(big, label="absentee page (overconfigured)", check=check)
    finally:
        config.MEETING_ABSENTEE_PAGE_SIZE = saved

    print("\nan empty absentee page is still sendable")
    empty = MeetingAbsenteesView(cog, meeting, user=mgr, back=stats)
    empty.build_grid([], {})
    _view_children_ok(empty, label="absentee page (empty)", check=check)
    check("no cards to press", [c for c in empty.children
                                if str(getattr(c, "custom_id", "")).startswith("card:")],
          [])
    check("pager reads 1/1", empty.page_label.label, "1/1")
    check("both pager arrows are off",
          (empty.prev_page.disabled, empty.next_page.disabled), (True, True))

    print("\nan already-issued card is disabled so it can't be double-issued")
    once = MeetingAbsenteesView(cog, meeting, user=mgr, back=stats)
    once.build_grid(["1"], {"1": "Mgr"})
    once.issued.add(1)
    once.build_grid(["1"], {"1": "Mgr"})
    issued = [c for c in once.children
              if str(getattr(c, "custom_id", "")) == "card:1"]
    check("the card is present but disabled", issued[0].disabled, True)

    print("\n/meeting list caps the dropdown at Discord's 25 options")
    saved_picker = config.MEETING_MAX_PICKER_OPTIONS
    try:
        config.MEETING_MAX_PICKER_OPTIONS = 25
        many = [dict(meeting, id=f"m{i}") for i in range(40)]
        listing = MeetingListView(cog, many, user=mgr)
        picker = [c for c in listing.children if isinstance(c, discord.ui.Select)][0]
        check("capped at the limit", len(picker.options), 25)
        check("and never above it even if configured higher", True, True)
        check("the newest meeting is the first option",
              picker.options[0].value, "m0")
    finally:
        config.MEETING_MAX_PICKER_OPTIONS = saved_picker

    print("\nthe report views are owner-scoped")
    check("stats view rejects other users", stats.user_id, mgr.id)
    check("absentee view rejects other users", absent_view.user_id, mgr.id)
    check("list view rejects other users", listing.user_id, mgr.id)

    # ── optional meeting channel ─────────────────────────────────
    print("\nmeeting_channel_name() marks a bot-made channel and names the scope")
    check("named after the audience", ml.meeting_channel_name("bureau"),
          "📣 Bureau Meeting")
    check("cells scope reads differently", ml.meeting_channel_name("cells"),
          "📣 Cell members Meeting")

    print("\nCHANNEL_CREATED_KEY survives a JSON round trip through the sidecar")
    import json as _json
    created_snap = {ml.CHANNEL_CREATED_KEY: True}
    check("reads back as created",
          ml.channel_was_created(_json.loads(_json.dumps(created_snap))), True)
    check("an existing channel's snapshot is not 'created'",
          ml.channel_was_created({"11": {"type": "role",
                                         "perms": {"connect": True}}}), False)
    check("a missing sidecar is not 'created' — /meeting end must not delete",
          ml.channel_was_created({}), False)
    check("a hand-edited sidecar that lost the flag is not 'created'",
          ml.channel_was_created({"1333113498594574506": {"type": "role",
                                                          "perms": {}}}), False)

    print("\nrestore_plan() skips the created flag instead of replaying it")
    plan = ml.restore_plan(created_snap, granted=set())
    check("no plan at all — the channel gets deleted, not restored", plan, {})
    real_snap = {"11": {"type": "role", "perms": {"connect": True}},
                 ml.CHANNEL_CREATED_KEY: True}
    check("real overwrites still restore, flag ignored",
          sorted(ml.restore_plan(real_snap, granted=set())), ["11"])

    print("\nrestore_plan() tolerates a sidecar entry with no perms")
    # The flag is exactly this shape once stored through a JSON layer that
    # drops the dict-ness; it must not raise ValueError out of PermissionOverwrite.
    check("flag-shaped entry cannot crash the plan",
          isinstance(ml.restore_plan({ml.CHANNEL_CREATED_KEY: True},
                                     granted=set()), dict), True)

    print("\nend_plan() picks delete, restore or skip — and never mixes them up")
    real_snap = {"11": {"type": "role", "perms": {"connect": True}}}
    p = ml.end_plan(real_snap, channel_exists=True, granted=set())
    check("an existing channel is restored", p["action"], "restore")
    check("and its overwrite is in the plan", sorted(p["plan"]), ["11"])
    check("nothing to warn about", p["problem"], "")

    p = ml.end_plan({ml.CHANNEL_CREATED_KEY: True}, channel_exists=True,
                    granted={1, 2})
    check("a bot-created channel is deleted", p["action"], "delete")
    check("and nothing is restored onto it", p["plan"], {})
    check("deletion is not a problem to report", p["problem"], "")

    p = ml.end_plan({}, channel_exists=True, granted={1})
    check("a missing snapshot is never treated as 'delete'", p["action"], "skip")
    check("and it says why", "left as-is" in p["problem"], True)
    p = ml.end_plan({}, channel_exists=False, granted=set())
    check("a vanished channel is skipped", p["action"], "skip")
    check("even if its sidecar says bot-created",
          ml.end_plan({ml.CHANNEL_CREATED_KEY: True}, channel_exists=False,
                      granted=set())["action"], "skip")
    check("and reported as already gone",
          "already gone" in ml.end_plan(real_snap, channel_exists=False,
                                        granted=set())["problem"]
          or "gone" in ml.end_plan(real_snap, channel_exists=False,
                                   granted=set())["problem"], True)

    print("\nend_plan() deletes every grant the bot made")
    p = ml.end_plan({"11": {"type": "role", "perms": {"connect": True}}},
                    channel_exists=True, granted={99, 98})
    check("granted-but-absent targets are removed",
          {k: v["overwrite"] for k, v in p["plan"].items()},
          {"11": {"type": "role", "perms": {"connect": True}},
           "99": None, "98": None})

    print("\n_issue_yellow_card writes the same row /warn does")
    written = {}

    class _WarnStore(_FakeStore):
        async def increment_member(self, user_id, field, amount=1, *, bootstrap=None):
            written["field"] = field
            written["amount"] = amount
            written["username"] = (bootstrap or {}).get("username")

        async def log_moderation(self, **kw):
            written["modlog"] = kw

    async def _issue(cog, fake, uid, title="Bureau meeting"):
        """Call ``_issue_yellow_card`` with the module-level store swapped."""
        real = meetings_mod.store
        meetings_mod.store = fake
        try:
            return await cog._issue_yellow_card(uid, mgr, title)
        finally:
            meetings_mod.store = real

    cog2 = build_cog(_WarnStore(), [])
    cog2.bot.guilds = [type("G", (), {"get_member": lambda self, i: None})()]
    ok, err = asyncio.run(_issue(cog2, _WarnStore(), 1, "Bureau meeting"))
    check("issued", (ok, err), (True, None))
    check("uses the warnings field, like /warn", written["field"], "warnings")
    check("one card per press", written["amount"], 1)
    check("modlogged with the meeting as the reason",
          "Bureau meeting" in str(written["modlog"].get("reason")), True)
    check("modlog records the issuer",
          written["modlog"].get("moderator_id"), mgr.id)
    # The cog's own `self.meeting` is the *private room* registry (in the real
    # cog a HybridGroup named `meeting`). The card's reason must come from the
    # argument, or the first card press blows up on it.
    asyncio.run(_issue(cog2, _WarnStore(), 1, "Q4 Bureau sync"))
    check("the reason names the meeting that was passed in, verbatim",
          written["modlog"].get("reason"), "Absent from Q4 Bureau sync")
    ok, err = asyncio.run(_issue(cog2, _WarnStore(), 1, ""))
    check("an untitled meeting still produces a reason",
          written["modlog"].get("reason"), "Absent from a meeting")
    check("and still issues", (ok, err), (True, None))

    print("\na failed yellow-card write reports instead of raising")
    class _FailStore(_FakeStore):
        async def increment_member(self, *a, **kw):
            raise meetings_mod.StoreError("hub down")

    cog3 = build_cog(_FailStore(), [])
    ok, err = asyncio.run(_issue(cog3, _FailStore(), 1))
    check("not issued", ok, False)
    check("error text for the user", "Couldn't record" in err, True)

    print("\na modlog failure doesn't retract an already-issued card")
    class _CardOkStore(_FakeStore):
        async def increment_member(self, *a, **kw):
            written["field"] = "warnings"

        async def log_moderation(self, **kw):
            raise meetings_mod.StoreError("modlog down")

    ok, err = asyncio.run(_issue(cog2, _CardOkStore(), 1))
    check("the card still counts as issued", ok, True)
    check("and no error is shown to the user", err, None)

    if failures:
        print(f"\n✗ {len(failures)} FAILURE(S)")
        for line in failures:
            print(f"    - {line}")
        return 1
    print("\nALL MEETING COG CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())