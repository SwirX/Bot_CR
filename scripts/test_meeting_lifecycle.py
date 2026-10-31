"""Offline checks for scheduled activation and empty-room shutdown."""

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs import _meetings  # noqa: E402
from cogs import meetings as meeting_module  # noqa: E402
from cogs.meetings import Meetings  # noqa: E402


class MeetingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_permission_grants_remain_visible_to_rollback(self):
        class Target:
            def __init__(self, identifier):
                self.id = identifier

        class Channel:
            def __init__(self):
                self.guild = SimpleNamespace(default_role=Target(0))
                self.calls = 0

            async def set_permissions(self, _target, **_kwargs):
                self.calls += 1
                if self.calls == 3:
                    raise RuntimeError("Discord rejected the second member")

        written = set()
        with self.assertRaises(RuntimeError):
            await _meetings.apply_meeting_permissions(
                Channel(), audience=[Target(1), Target(2)], written=written)
        self.assertEqual(len(written), 2)
        self.assertIn("0", written)

    async def test_restart_reclaims_prepared_voice_room(self):
        cog = Meetings(SimpleNamespace())
        saved = json.dumps([{"channel_id": 8, "prepared": True}])
        with patch.object(meeting_module.store, "get_setting",
                          new=AsyncMock(return_value=saved)):
            await cog._load_schedules()
        self.assertFalse(cog._scheduled[0]["prepared"])

    def test_scheduler_wakes_at_start_time(self):
        cog = Meetings(SimpleNamespace())
        cog._scheduled = [{"starts_at": (datetime.now(timezone.utc)
                                          + timedelta(seconds=3)).isoformat(),
                           "prepared": True}]
        self.assertGreater(cog._next_schedule_delay(), 0)
        self.assertLessEqual(cog._next_schedule_delay(), 3)
        cog._scheduled[0]["prepared"] = False
        self.assertLessEqual(cog._next_schedule_delay(), 3)

    async def test_chief_default_audience_stays_inside_their_cell(self):
        def member(uid, role):
            return SimpleNamespace(id=uid, display_name=str(uid), bot=False,
                                   roles=[SimpleNamespace(name=role, managed=False)])

        chief = member(1, "Head of IT Unit")
        own = member(2, "Member of IT Unit")
        other = member(3, "Member of Technical Unit")
        president = member(4, "President")
        guild = SimpleNamespace(members=[chief, own, other, president])
        audience = await _meetings.resolve_audience(guild, "my_cell", organizer=chief)
        self.assertEqual({person.id for person in audience}, {1, 2, 4})

    async def test_combined_communication_chief_covers_both_cells(self):
        def member(uid, role):
            return SimpleNamespace(id=uid, display_name=str(uid), bot=False,
                                   roles=[SimpleNamespace(name=role, managed=False)])

        chief = member(1, "Head of Communication & Docs Unit")
        communication = member(2, "Member of Communication Unit")
        documentation = member(3, "Member of Documentation Unit")
        unrelated = member(4, "Member of Technical Unit")
        guild = SimpleNamespace(members=[chief, communication, documentation, unrelated])
        audience = await _meetings.resolve_audience(guild, "my_cell", organizer=chief)
        self.assertEqual({person.id for person in audience}, {1, 2, 3})

    async def test_schedule_prepares_voice_ten_minutes_before_start(self):
        guild = SimpleNamespace(id=7)
        channel = SimpleNamespace(id=8, guild=guild)
        guild.get_channel = lambda _id: channel
        bot = SimpleNamespace(get_guild=lambda _id: guild)
        cog = Meetings(bot)
        cog._live_meeting = AsyncMock(return_value=None)
        cog._claim_voice = AsyncMock()
        cog._save_schedules = AsyncMock()
        cog._scheduled = [{"guild_id": 7, "channel_id": 8,
                           "starts_at": (datetime.now(timezone.utc)
                                         + timedelta(minutes=9)).isoformat(),
                           "prepared": False}]
        await cog._advance_schedules()
        cog._claim_voice.assert_awaited_once_with(channel)
        self.assertTrue(cog._scheduled[0]["prepared"])
        await cog._advance_schedules()
        cog._claim_voice.assert_awaited_once()

    async def test_due_schedule_starts_in_its_created_channel(self):
        guild = SimpleNamespace(id=7)
        channel = SimpleNamespace(id=8, guild=guild)
        guild.get_channel = lambda _id: channel
        guild.get_member = lambda _id: SimpleNamespace(
            id=9, bot=False, roles=[SimpleNamespace(name="Manager", managed=False)])
        guild.get_role = lambda _id: None
        cog = Meetings(SimpleNamespace(get_guild=lambda _id: guild))
        cog._live_meeting = AsyncMock(return_value=None)
        cog._live_meeting_for_channel = AsyncMock(return_value={"id": "meeting"})
        cog._save_schedules = AsyncMock()
        item = {"guild_id": 7, "channel_id": 8, "organizer_id": 9,
                "starts_at": (datetime.now(timezone.utc)
                              - timedelta(minutes=1)).isoformat(),
                "prepared": True, "audience": "bureau", "minutes": 30,
                "role_id": None}
        cog._scheduled = [item]
        calls = []

        async def activate(_cog, ctx, meeting_channel, **_kwargs):
            calls.append((ctx.created_channel_id, meeting_channel.id))

        with patch.object(cog, "meeting_start", SimpleNamespace(callback=activate)):
            await cog._advance_schedules()
        self.assertEqual(calls, [(8, 8)])
        self.assertEqual(cog._scheduled, [])

    async def test_revoked_organizer_cannot_start_scheduled_meeting(self):
        guild = SimpleNamespace(id=7)
        channel = SimpleNamespace(id=8, guild=guild)
        guild.get_channel = lambda _id: channel
        guild.get_member = lambda _id: SimpleNamespace(id=9, bot=False, roles=[])
        cog = Meetings(SimpleNamespace(get_guild=lambda _id: guild))
        cog._live_meeting = AsyncMock(return_value=None)
        cog._save_schedules = AsyncMock()
        cog._discard_channel = AsyncMock()
        cog._scheduled = [{"guild_id": 7, "channel_id": 8, "organizer_id": 9,
                           "starts_at": (datetime.now(timezone.utc)
                                         - timedelta(minutes=1)).isoformat(),
                           "prepared": True}]
        await cog._advance_schedules()
        self.assertEqual(cog._scheduled, [])
        cog._discard_channel.assert_awaited_once_with(channel)

    async def test_empty_room_closes_after_ten_minutes(self):
        guild = SimpleNamespace(id=7)
        channel = SimpleNamespace(id=8, guild=guild, members=[])
        cog = Meetings(SimpleNamespace())
        cog._live_meeting_for_channel = AsyncMock(return_value={"id": "meeting"})
        close = cog._end_running_meeting = AsyncMock()
        with patch("cogs.meetings.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await cog._end_after_idle(channel)
        sleep.assert_awaited_once_with(600)
        close.assert_awaited_once()

    async def test_returned_member_prevents_idle_close(self):
        channel = SimpleNamespace(id=8, guild=SimpleNamespace(id=7),
                                  members=[SimpleNamespace(bot=False)])
        cog = Meetings(SimpleNamespace())
        cog._live_meeting_for_channel = AsyncMock()
        with patch("cogs.meetings.asyncio.sleep", new_callable=AsyncMock):
            await cog._end_after_idle(channel)
        cog._live_meeting_for_channel.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
