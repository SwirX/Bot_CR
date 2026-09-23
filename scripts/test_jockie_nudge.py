"""Hermetic checks for the JockieMusic migration nudge.

Run:  .venv-local/bin/python scripts/test_jockie_nudge.py
No Discord connection; message objects are minimal fakes.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.jockie_nudge import JockieNudge  # noqa: E402


class _Author:
    def __init__(self, user_id: int, is_bot: bool = False):
        self.id = user_id
        self.bot = is_bot


class _Channel:
    def __init__(self, channel_id: int):
        self.id = channel_id


class _Guild:
    pass


class _Message:
    """Stand-in for discord.Message: records pitches sent via reply()."""

    def __init__(self, content: str, user_id: int, channel_id: int,
                 *, is_bot: bool = False):
        self.content = content
        self.author = _Author(user_id, is_bot)
        self.channel = _Channel(channel_id)
        self.guild = _Guild()
        self.replies: list[str] = []

    async def reply(self, content: str) -> None:
        self.replies.append(content)


class JockieNudgeTests(unittest.TestCase):
    def _dispatch(self, message: _Message) -> JockieNudge:
        cog = JockieNudge(None)
        asyncio.run(cog.on_message(message))
        return cog

    def test_jockie_play_command_gets_pitch(self):
        message = _Message("m!play shape of you", user_id=1, channel_id=10)
        self._dispatch(message)
        self.assertEqual(len(message.replies), 1)
        self.assertIn("/play", message.replies[0])
        self.assertIn("/queue auto", message.replies[0])

    def test_uppercase_prefix_also_matches(self):
        message = _Message("M!queue", user_id=1, channel_id=10)
        self._dispatch(message)
        self.assertEqual(len(message.replies), 1)

    def test_plain_text_without_prefix_is_ignored(self):
        message = _Message("!play shape of you", user_id=1, channel_id=10)
        self._dispatch(message)
        self.assertEqual(len(message.replies), 0)

    def test_bare_prefix_is_ignored(self):
        message = _Message("m!", user_id=1, channel_id=10)
        self._dispatch(message)
        self.assertEqual(len(message.replies), 0)

    def test_bot_messages_are_ignored(self):
        message = _Message("m!play", user_id=1, channel_id=10, is_bot=True)
        self._dispatch(message)
        self.assertEqual(len(message.replies), 0)

    def test_user_cooldown_suppresses_quick_repeat(self):
        first = _Message("m!play song", user_id=1, channel_id=10)
        cog = self._dispatch(first)
        second = _Message("m!skip", user_id=1, channel_id=10)
        asyncio.run(cog.on_message(second))
        self.assertEqual(len(first.replies), 1)
        self.assertEqual(len(second.replies), 0)

    def test_channel_cooldown_applies_across_users(self):
        first = _Message("m!play song", user_id=1, channel_id=10)
        cog = self._dispatch(first)
        other = _Message("M!play song", user_id=2, channel_id=10)
        asyncio.run(cog.on_message(other))
        self.assertEqual(len(other.replies), 0)
        distant = _Message("m!play song", user_id=2, channel_id=11)
        asyncio.run(cog.on_message(distant))
        self.assertEqual(len(distant.replies), 1)


if __name__ == "__main__":
    unittest.main()