#!/usr/bin/env python3
"""Round-trip test for the meeting / attendance store layer.

Talks to the real robotics_hub (the tables are provisioned), writes a meeting
with two join/leave/rejoin cycles, then asserts the shape the cog and the CSV
export depend on. Everything it creates is deleted in the ``finally`` block.

Usage:
    python scripts/test_meeting_store.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.store import store, StoreError  # noqa: E402

ALICE, BOB = "111111111111111111", "222222222222222222"


async def main() -> int:
    await store.init()
    started = datetime.now(timezone.utc)
    mid = None
    try:
        mid = await store.create_meeting({
            "title": "Bureau meeting",
            "channel_id": "1336692513460977746",
            "channel_name": "Bureau",
            "scope": "bureau",
            "started_at": started.isoformat(),
            "planned_minutes": 45,
            "expected": [ALICE, BOB],
            "created_by": None,
        })
        print(f"created meeting {mid}")

        got = await store.get_meeting(mid)
        assert got and got["live"] is True, got
        assert got["expected"] == [ALICE, BOB], got["expected"]
        assert got["planned_minutes"] == 45, got
        assert got["ended_at"] == "", got
        print("  read back ok:", got["scope"], got["title"], got["planned_minutes"], "min")

        # live-meeting lookups (the voice listener uses both)
        by_chan = await store.get_live_meeting_for_channel(1336692513460977746)
        assert by_chan and by_chan["id"] == mid, by_chan
        assert (await store.get_live_meeting_for_channel(1)) is None
        live = await store.get_live_meeting()
        assert live and live["id"] == mid, live
        print("  live lookups ok")

        # alice: join -> leave -> rejoin (two rows), bob: join -> still open
        t0 = started + timedelta(minutes=5)
        await store.record_meeting_join(mid, ALICE, "Alice", scope_note="bureau")
        await asyncio.sleep(0.05)
        assert await store.close_meeting_session(mid, ALICE), "alice left"
        await asyncio.sleep(0.05)
        await store.record_meeting_join(mid, ALICE, "Alice", scope_note="bureau")
        await store.record_meeting_join(mid, BOB, "Bob", scope_note="bureau")

        sessions = await store.list_meeting_sessions(mid)
        assert len(sessions) == 3, sessions
        alice = [s for s in sessions if s["discord_user"] == ALICE]
        assert len(alice) == 2, "alice must have 2 attendance rows"
        assert alice[0]["left_at"], "first visit is closed"
        assert alice[1]["left_at"] == "" and alice[1]["open"], "rejoin still open"
        assert [s["joined_at"] for s in sessions] == sorted(s["joined_at"] for s in sessions)
        print("  attendance ok: alice x2 (one closed, one open), bob x1")

        # duplicate leave must not re-close a closed row
        assert not await store.close_meeting_session(mid, ALICE + "3")
        assert await store.close_meeting_session(mid, BOB)
        assert not await store.close_meeting_session(mid, BOB), "no double-close"
        print("  leave guard ok")

        # lock + per-member re-entry grant
        await store.set_meeting_locked(mid, True)
        assert (await store.get_meeting(mid))["locked"] is True
        await store.grant_meeting_reentry(mid, ALICE)
        await store.grant_meeting_reentry(mid, ALICE)  # idempotent
        granted = (await store.get_meeting(mid))["granted"]
        assert granted == [ALICE], granted
        print("  lock + grant ok:", granted)

        # channel-permission snapshot sidecar round-trip
        snap = {"overwrites": [{"id": "1", "type": 0, "allow": "8", "deny": "0"}]}
        await store.save_meeting_channel_state(mid, snap)
        assert await store.meeting_channel_state(mid) == snap
        assert await store.meeting_channel_state("does-not-exist") == {}
        print("  channel snapshot sidecar ok")

        # end + history
        await store.end_meeting(mid, at=started + timedelta(hours=1))
        ended = await store.get_meeting(mid)
        assert ended["live"] is False and ended["ended_at"], ended
        assert await store.get_live_meeting_for_channel(1336692513460977746) is None
        assert (await store.latest_meeting())["id"] == mid
        assert (await store.get_live_meeting()) is None
        print("  end ok, duration anchor", ended["ended_at"])

        hist = await store.list_meetings(5)
        assert hist[0]["id"] == mid
        assert await store.list_meetings(5, live=True) == []
        assert len(await store.list_meetings(5, live=False)) == 1
        print("  history ok")

        print("\nALL MEETING STORE CHECKS PASSED")
        return 0
    finally:
        if mid:
            # meetings -> meeting_sessions is ON DELETE CASCADE, so one delete
            # cleans up the attendance rows too.
            await _drop(mid)


async def _drop(mid: str) -> None:
    try:
        store._tdb.delete_row(database_id=store._db_id,
                              table_id="meetings", row_id=mid)
        print(f"cleaned up meeting {mid}")
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        print(f"cleanup failed for {mid}: {exc}")


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (AssertionError, StoreError) as exc:
        print(f"FAIL: {exc}")
        raise SystemExit(1) from exc
