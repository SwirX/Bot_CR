"""Hermetic checks for content-driven XP bonuses.

Run:  .venv-local/bin/python scripts/test_xp_bonuses.py
No network: the richness scorer runs on fake messages/attachments; the
cooldown-gated on_message path is exercised with the store read mocked.
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from cogs.engagement import Engagement  # noqa: E402
from data.store import store as real_store  # noqa: E402


class _Attachment:
    def __init__(self, *, content_type="", filename=""):
        self.content_type = content_type
        self.filename = filename

    def is_voice_message(self):
        return self.content_type == "audio/ogg" and self.filename.endswith(".ogg")


class _Author:
    id = 7
    name = "tester"
    mention = "<@7>"
    bot = False
    guild = None
    roles = []


class _Message:
    def __init__(self, *, content="", attachments=()):
        self.content = content
        self.attachments = list(attachments)
        self.author = _Author()
        self.guild = object()
        self.channel = mock.Mock()


class BonusTests(unittest.TestCase):
    def setUp(self):
        self.old = {
            name: getattr(config, name)
            for name in ("XP_BONUS_IMAGE", "XP_BONUS_LINK", "XP_BONUS_LONG_MSG",
                         "XP_LONG_MSG_WORDS", "XP_BONUS_VOICE_NOTE")
        }
        self.cog = Engagement.__new__(Engagement)

    def tearDown(self):
        for name, value in self.old.items():
            setattr(config, name, value)

    def test_plain_message_earns_nothing_extra(self):
        msg = _Message(content="hey")
        self.assertEqual(self.cog._richness_bonus(msg), 0)

    def test_image_mime_type_counts(self):
        msg = _Message(attachments=[_Attachment(content_type="image/jpeg", filename="a.jpg")])
        self.assertEqual(self.cog._richness_bonus(msg), config.XP_BONUS_IMAGE)

    def test_image_by_extension_without_mime(self):
        msg = _Message(attachments=[_Attachment(filename="photo.png")])
        self.assertEqual(self.cog._richness_bonus(msg), config.XP_BONUS_IMAGE)

    def test_voice_note_counts(self):
        msg = _Message(attachments=[_Attachment(content_type="audio/ogg", filename="vm.ogg")])
        self.assertEqual(self.cog._richness_bonus(msg), config.XP_BONUS_VOICE_NOTE)

    def test_voice_note_does_not_double_count_as_image(self):
        msg = _Message(attachments=[_Attachment(content_type="audio/ogg", filename="vm.ogg")])
        self.assertEqual(
            self.cog._richness_bonus(msg),
            config.XP_BONUS_VOICE_NOTE,  # NOT image bonus on top
        )

    def test_link_counts(self):
        for text in ("check https://robotics.ma", "see www.example.com"):
            self.assertEqual(
                self.cog._richness_bonus(_Message(content=text)),
                config.XP_BONUS_LINK,
            )

    def test_long_message_counts_and_short_does_not(self):
        words = "word " * 60
        self.assertEqual(self.cog._richness_bonus(_Message(content=words)),
                         config.XP_BONUS_LONG_MSG)
        self.assertEqual(self.cog._richness_bonus(_Message(content="word " * 10)), 0)

    def test_bonuses_stack(self):
        msg = _Message(
            content="https://robotics.ma " + "word " * 60,
            attachments=[_Attachment(content_type="image/png", filename="a.png")],
        )
        expected = config.XP_BONUS_IMAGE + config.XP_BONUS_LINK + config.XP_BONUS_LONG_MSG
        self.assertEqual(self.cog._richness_bonus(msg), expected)


class OnMessageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.old_min = config.XP_MIN
        self.old_max = config.XP_MAX
        config.XP_MIN = config.XP_MAX = 5   # deterministic base roll
        self.cog = Engagement.__new__(Engagement)
        self.cog.bot = mock.Mock()
        self.cog.xp_pending = {}
        self.cog.xp_base = {}
        self.cog.level_seen = {}
        self.cog.name_cache = {}
        self.cog.xp_cooldown = {}
        self.cog.voice_sessions = {}
        self.cog.voice_day_granted = {}
        self.cog._voice_per_second = 0.0
        self._store_patch = mock.patch.object(real_store, "get_member", new=mock.AsyncMock(return_value=None))
        self._store_patch.start()

    def tearDown(self):
        self._store_patch.stop()
        config.XP_MIN = self.old_min
        config.XP_MAX = self.old_max

    def _msg(self, *, content="", attachments=()):
        return _Message(content=content, attachments=attachments)

    def test_on_message_grants_base_plus_bonus(self):
        msg = self._msg(attachments=[_Attachment(content_type="image/jpeg", filename="a.jpg")])
        asyncio.run(self.cog.on_message(msg))
        self.assertEqual(self.cog.xp_pending.get(7), 5 + config.XP_BONUS_IMAGE)

    def test_on_message_plain_grants_only_base(self):
        asyncio.run(self.cog.on_message(self._msg(content="hey")))
        self.assertEqual(self.cog.xp_pending.get(7), 5)


if __name__ == "__main__":
    unittest.main()