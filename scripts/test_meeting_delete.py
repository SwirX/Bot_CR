import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cogs._meeting_delete import delete_finished_meeting
from data.store import StoreError


class DeleteMeetingTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_attendance_before_meeting_and_sidecar(self):
        client = SimpleNamespace(delete_row=MagicMock())
        fake = SimpleNamespace(get_meeting=AsyncMock(return_value={"live": False}),
                               _raw=lambda: (client, "database"),
                               _list=AsyncMock(side_effect=[[{"$id": "s1"}, {"$id": "s2"}], []]))
        with patch("cogs._meeting_delete.store", fake):
            await delete_finished_meeting("m1")
        self.assertEqual([(call.args[1], call.args[2]) for call in client.delete_row.call_args_list],
                         [("meeting_sessions", "s1"), ("meeting_sessions", "s2"),
                          ("bot_settings", "meeting_side.m1"), ("meetings", "m1")])

    async def test_refuses_live_meeting_before_any_deletion(self):
        fake = SimpleNamespace(get_meeting=AsyncMock(return_value={"live": True}), _raw=MagicMock())
        with patch("cogs._meeting_delete.store", fake):
            with self.assertRaises(StoreError):
                await delete_finished_meeting("m1")
        fake._raw.assert_not_called()
