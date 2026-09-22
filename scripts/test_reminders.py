"""Hermetic checks for the reminders cog (span parsing + scheduler delivery).

Run:  .venv-local/bin/python scripts/test_reminders.py
No network: the cog is built bare (bypassing __init__), the store is mocked
and delivery targets a fake user, so nothing touches Discord or Appwrite.
"""

import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs._dates import parse_span  # noqa: E402
from cogs.reminders import Reminders, _iso_at, fmt_span  # noqa: E402
from data.store import store as real_store  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class SpanTests(unittest.TestCase):
    def test_parses_units(self):
        self.assertEqual(parse_span("45m"), 45 * 60)
        self.assertEqual(parse_span("2h"), 7200)
        self.assertEqual(parse_span("1d"), 86400)
        self.assertEqual(parse_span("1w"), 604800)
        self.assertEqual(parse_span("90s"), 90)

    def test_parses_compound_spans(self):
        self.assertEqual(parse_span("1h30m"), 5400)
        self.assertEqual(parse_span("1h 30m"), 5400)
        self.assertEqual(parse_span("2h5m10s"), 7510)

    def test_rejects_garbage(self):
        for bad in ("", "abc", "10", "10x", "30 minutes"):
            with self.assertRaises(ValueError):
                parse_span(bad)

    def test_fmt_span(self):
        self.assertEqual(fmt_span(5400), "1h 30m")
        self.assertEqual(fmt_span(3725), "1h 2m 5s")
        self.assertEqual(fmt_span(0), "0s")
        self.assertEqual(fmt_span(86400), "1d")


class User:
    def __init__(self, uid):
        self.id = uid
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append(content)


class StubBot:
    def __init__(self, user):
        self._user = user

    def get_user(self, uid):
        return self._user if self._user.id == uid else None

    async def fetch_user(self, uid):
        if self._user and self._user.id == uid:
            return self._user
        import discord
        from unittest import mock

        raise discord.NotFound(mock.Mock(status=404), "user not found")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.user = User(42)
        self.cog = Reminders.__new__(Reminders)
        self.cog.bot = StubBot(self.user)
        self.cog._loaded = True
        self.cog._pending = []
        self.save_patch = mock.patch.object(real_store, "set_setting", new=mock.AsyncMock())
        self.save_mock = self.save_patch.start()

    def tearDown(self):
        self.save_patch.stop()

    def test_iso_at_is_ordered(self):
        self.assertLess(_iso_at(0), _iso_at(60))

    def test_delivers_due_reminders_and_clears_them(self):
        # Delivery is one-shot: an unreachable user (gone / blocked bot) is
        # discarded too — retrying can never succeed, so it must not linger.
        self.cog._pending = [
            {"id": "abc1", "user_id": "42", "due_at": _iso_at(-5), "text": "stand up"},
            {"id": "abc2", "user_id": "42", "due_at": _iso_at(3600), "text": "later"},
            {"id": "abc3", "user_id": "99", "due_at": _iso_at(-5), "text": "other user"},
        ]
        _run(self.cog._poll())
        self.assertEqual(self.user.sent, ["⏰ **Reminder:** stand up"])
        self.assertEqual([r["id"] for r in self.cog._pending], ["abc2"])
        self.save_mock.assert_awaited_once()

    def test_no_due_reminders_do_not_persist(self):
        self.cog._pending = [
            {"id": "zz", "user_id": "42", "due_at": _iso_at(600), "text": "not yet"},
        ]
        _run(self.cog._poll())
        self.assertEqual(self.user.sent, [])
        self.save_mock.assert_not_awaited()

    def test_list_and_cancel_are_owner_scoped(self):
        self.cog._pending = [
            {"id": "mine", "user_id": "42", "due_at": _iso_at(600), "text": "mine"},
            {"id": "theirs", "user_id": "99", "due_at": _iso_at(600), "text": "theirs"},
        ]
        mine = self.cog._mine(42)
        self.assertEqual([r["id"] for r in mine], ["mine"])
        with mock.patch("cogs.reminders.store.set_setting", new=mock.AsyncMock()):
            self.cog._pending = [r for r in self.cog._pending if r["id"] != "mine"]
        self.assertEqual([r["id"] for r in self.cog._pending], ["theirs"])

    def test_load_ignores_corrupt_payload(self):
        with mock.patch.object(real_store, "get_setting", new=mock.AsyncMock(return_value="not json{")):
            _run(self.cog._load())
        self.assertEqual(self.cog._pending, [])

    def test_load_filters_malformed_rows(self):
        payload = json.dumps([
            {"id": "ok", "user_id": "1", "due_at": "2026-09-30T00:00:00+00:00", "text": "fine"},
            {"user_id": "2", "due_at": "2026-09-30T00:00:00+00:00"},   # no id/text
            "junk",
        ])
        with mock.patch.object(real_store, "get_setting", new=mock.AsyncMock(return_value=payload)):
            _run(self.cog._load())
        self.assertEqual([r["id"] for r in self.cog._pending], ["ok"])


if __name__ == "__main__":
    unittest.main()