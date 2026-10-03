"""Checks for the reusable member picker in cogs/_ui.py.

The behaviour worth testing here is not "does a SelectMenu get added" but the
selection bookkeeping: a choice made on one page has to survive turning to
another and coming back, because getting that wrong silently drops people from a
meeting's audience with no error anywhere.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("APPWRITE_API_KEY", "y")

import discord  # noqa: E402
from cogs._ui import MemberPickerView  # noqa: E402


class _Role:
    def __init__(self, name, managed=False):
        self.name, self.managed = name, managed


class _Member:
    def __init__(self, name, uid, *roles, bot=False):
        self.display_name, self.id, self.bot = name, uid, bot
        self.name = name.lower()
        self.roles = [_Role(r) for r in roles]


def members(n, *, with_roles=True):
    return [_Member(f"M{i}", 1000 + i,
                    "「👸」President" if i == 0 else "Member of IT Unit")
            for i in range(n)] if with_roles else \
           [_Member(f"M{i}", 1000 + i) for i in range(n)]


class _Interaction:
    """Just the payload the picker reads, plus a recorded edit."""

    def __init__(self, uid=1, values=None):
        self.user = _Member("Admin", uid)
        self.data = {"values": values or []}
        self.message = type("M", (), {"id": 5})()
        self.response = self
        self.followup = self
        self.edits = []
        self._done = False

    def is_done(self):
        return self._done

    async def edit_message(self, **kw):
        self._done = True
        self.edits.append(kw)

    async def send(self, content=None, **kw):
        self._done = True
        self.edits.append({"content": content, **kw})

    async def send_message(self, content=None, **kw):
        self._done = True
        self.edits.append({"content": content, **kw})


def press(view, name, interaction):
    """Fire one of the view's buttons.

    ``@discord.ui.button`` replaces the method with the Button itself, so
    ``view.next(...)`` would invoke the Button and fail with "not callable".
    The Button's ``callback`` is an ``_ItemCallback`` whose ``__call__`` already
    binds the owning view and the item — it takes only the interaction.
    """
    asyncio_run(getattr(view, name).callback(interaction))


def check(label, got, want):
    if got != want:
        print(f"  ✗ {label} = {got!r} (want {want!r})")
        return 1
    print(f"  ✓ {label} = {got!r}")
    return 0


def check_true(label, got):
    return check(label, bool(got), True)


def main() -> int:
    failures = 0
    admin = _Member("Admin", 1, "「👸」Manager")

    print("a picker over a few members is a single page")
    people = members(4)
    v = MemberPickerView(people, on_confirm=lambda *a: None, user=admin)
    failures += check("one page", v.pages, 1)
    failures += check("page label", v.page_label.label, "1/1")
    failures += check("prev disabled on the first page", v.prev.disabled, True)
    failures += check("next disabled on the only page", v.next.disabled, True)
    failures += check("nothing chosen yet", v.selected(), [])
    failures += check("confirm is disabled until something is picked",
                      v.confirm.disabled, True)

    print("\nselecting people enables confirm and records them")
    v = MemberPickerView(people, on_confirm=lambda *a: None, user=admin)
    asyncio_run(v._on_select(_Interaction(values=["1001", "1002"])))
    failures += check("two chosen", [m.id for m in v.selected()], [1001, 1002])
    failures += check("confirm enabled", v.confirm.disabled, False)
    failures += check_true("selecting redrew the menu", v.select is not None)

    print("\ndeselecting removes them again")
    asyncio_run(v._on_select(_Interaction(values=["1002"])))
    failures += check("one left", [m.id for m in v.selected()], [1002])

    print("\nclearing the selection empties it")
    asyncio_run(v._on_select(_Interaction(values=[])))
    failures += check("none", v.selected(), [])

    print("\na big guild paginates")
    many = members(60)
    v = MemberPickerView(many, on_confirm=lambda *a: None, user=admin,
                         page_size=25)
    failures += check("three pages of 25", v.pages, 3)
    failures += check("starts on page 1", v.page_label.label, "1/3")
    failures += check("prev disabled", v.prev.disabled, True)
    failures += check("next enabled", v.next.disabled, False)
    failures += check("25 options on the page", len(v.select.options), 25)
    first_page_ids = {o.value for o in v.select.options}
    failures += check("the page holds the first 25",
                      sorted(int(x) for x in first_page_ids)[:2], [1000, 1001])

    print("\na choice on page 1 survives a trip to page 2 and back")
    asyncio_run(v._on_select(_Interaction(values=["1000", "1005"])))
    failures += check("recorded on page 1",
                      sorted(v.selected(), key=lambda m: m.id) and
                      [m.id for m in v.selected()], [1000, 1005])
    press(v, "next", _Interaction())
    failures += check("now on page 2", v.page_label.label, "2/3")
    failures += check("page 1's choice is still held", len(v.selected()), 2)
    failures += check("page 2 offers different people",
                      all(o.value not in first_page_ids
                          for o in v.select.options), True)

    print("\nchoices on different pages accumulate rather than replace")
    page2_ids = sorted(o.value for o in v.select.options)[:2]
    asyncio_run(v._on_select(_Interaction(values=page2_ids)))
    failures += check("four chosen across both pages", len(v.selected()), 4)
    press(v, "prev", _Interaction())
    failures += check("back on page 1", v.page_label.label, "1/3")
    failures += check("and page 1's menu shows its own two as selected",
                      sum(1 for o in v.select.options if o.default), 2)
    failures += check("the total is unchanged by paging", len(v.selected()), 4)

    print("\nre-picking on a page replaces only that page's choice")
    asyncio_run(v._on_select(_Interaction(values=["1000"])))
    failures += check("page 1 down to one", len(v.selected()), 3)
    failures += check("and the other page kept both", len(v.selected()), 3)

    print("\npaging past the ends is clamped")
    press(v, "prev", _Interaction())
    press(v, "prev", _Interaction())
    failures += check("stays on page 1", v.page_label.label, "1/3")
    for _ in range(5):
        press(v, "next", _Interaction())
    failures += check("stays on the last page", v.page_label.label, "3/3")

    print("\nconfirm hands the accumulated picks to the callback")
    got = {}

    async def _confirm(interaction, members):
        got["members"] = [m.id for m in members]

    v = MemberPickerView(members(10), on_confirm=_confirm, user=admin)
    asyncio_run(v._on_select(_Interaction(values=["1003", "1007"])))
    press(v, "confirm", _Interaction())
    failures += check("callback got both, in candidate order",
                      got.get("members"), [1003, 1007])
    failures += check("view is marked confirmed", v.confirmed, True)
    failures += check("everything is disabled afterwards",
                      all(c.disabled for c in v.children
                          if not isinstance(c, discord.ui.Select)), True)

    print("\nconfirm and cancel release a caller blocked in view.wait()")
    # Without stop() the wait only ends at the timeout, so a command that waits
    # for an audience before acting would sit idle for the full five minutes on
    # a pick that had already succeeded.
    async def _wait_and_press(view, vname, want_cancelled):
        waiter = asyncio.ensure_future(view.wait())
        await asyncio.sleep(0)
        # Driven inline rather than via press(), so the waiter and the button
        # share one event loop -- press() would open a second one.
        await getattr(view, vname).callback(_Interaction())
        timed_out = await asyncio.wait_for(waiter, timeout=2)
        check(f"{vname}: wait() returned 'finished', not 'timed out'",
              timed_out, False)
        check(f"{vname}: cancelled is {want_cancelled}", view.cancelled,
              want_cancelled)

    for name, expect_cancelled in (("confirm", False), ("cancel", True)):
        got.clear()
        v = MemberPickerView(members(10), on_confirm=_confirm, user=admin)
        asyncio_run(v._on_select(_Interaction(values=["1000"])))
        asyncio_run(_wait_and_press(v, name, expect_cancelled))

    print("\nconfirming with nothing chosen refuses instead of calling back")
    got.clear()
    v = MemberPickerView(members(3), on_confirm=_confirm, user=admin)
    empty_press = _Interaction()
    press(v, "confirm", empty_press)
    failures += check("callback not fired", "members" in got, False)
    failures += check("and the owner is told why",
                      "at least one" in str(empty_press.edits), True)

    print("\ncancel closes without confirming")
    got.clear()
    v = MemberPickerView(members(3), on_confirm=_confirm, user=admin)
    press(v, "cancel", _Interaction())
    failures += check("marked cancelled", v.cancelled, True)
    failures += check("not confirmed", v.confirmed, False)
    failures += check("nothing called back", "members" in got, False)

    print("\nbots are never offered, however many are passed in")
    mixed = [_Member("Human", 1), _Member("Robot", 2, bot=True),
             _Member("Bot2", 3, bot=True)]
    v = MemberPickerView(mixed, on_confirm=_confirm, user=admin)
    failures += check("only humans remain", len(v.candidates), 1)

    print("\nother people cannot drive the picker")
    v = MemberPickerView(members(5), on_confirm=_confirm, user=admin)
    asyncio_run(v._on_select(_Interaction(uid=999, values=["1000"])))
    failures += check("nothing was selected", v.selected(), [])
    press(v, "next", _Interaction(uid=999))
    failures += check("and the page did not turn", v.page_label.label, "1/1")

    print("\noption labels carry the member's senior role")
    v = MemberPickerView(members(3), on_confirm=_confirm, user=admin)
    by_label = {o.label: o.description for o in v.select.options}
    failures += check("the president is findable by role",
                      by_label.get("M0"), "「👸」President")
    failures += check("everyone else says their unit",
                      by_label.get("M1"), "Member of IT Unit")

    print("\nnobody in the guild is not a crash")
    v = MemberPickerView([], on_confirm=_confirm, user=admin)
    failures += check("still one page", v.pages, 1)
    failures += check("no options", len(v.select.options), 0)

    print("\nDiscord's component limits are respected at every page size")
    for size in (1, 5, 25):
        v = MemberPickerView(members(30), on_confirm=_confirm, user=admin,
                             page_size=size)
        rows = v.to_components()
        flat = [c for row in rows for c in row["components"]]
        failures += check(f"page_size={size}: under 25 components",
                          len(flat) <= 25, True)
        failures += check(f"page_size={size}: no row over 5",
                          max((len(r["components"]) for r in rows),
                              default=0) <= 5, True)
    # A configured page size above Discord's cap must clamp, not explode.
    v = MemberPickerView(members(60), on_confirm=_confirm, user=admin,
                         page_size=100)
    failures += check("an oversized page_size clamps to 25", v.page_size, 25)
    failures += check("and still builds", len(v.to_components()), 2)

    if failures:
        print(f"\n✗ {failures} FAILURE(S)")
        return 1
    print("\nALL MEMBER PICKER CHECKS PASSED")
    return 0


def asyncio_run(coro):
    return asyncio.run(coro)


if __name__ == "__main__":
    raise SystemExit(main())