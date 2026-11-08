import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("APPWRITE_API_KEY", "y")

from cogs.lesson_reminder import LessonReminder, needs_reminder, reminder_text  # noqa: E402


def member(member_id, roles, *, nick=None, bot=False):
    return SimpleNamespace(id=member_id, roles=roles, nick=nick, bot=bot, send=AsyncMock())


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    def test_selection_requires_all_three_conditions_and_excludes_bots(self):
        pending = SimpleNamespace(id=10)
        new = SimpleNamespace(id=20)
        self.assertTrue(needs_reminder(member(1, [pending]), 10, 20))
        self.assertFalse(needs_reminder(member(2, []), 10, 20))
        self.assertFalse(needs_reminder(member(3, [pending, new]), 10, 20))
        self.assertFalse(needs_reminder(member(4, [pending], nick="𝓐𝓭𝓪"), 10, 20))
        self.assertFalse(needs_reminder(member(5, [pending], bot=True), 10, 20))

    def test_supported_preferences_and_french_fallback(self):
        self.assertIn("cours de langage C", reminder_text(None)[0])
        self.assertEqual(reminder_text("unknown"), reminder_text("fr"))
        self.assertIn("C language lesson", reminder_text("en")[0])
        self.assertIn("درس", reminder_text("ar")[0])

    async def test_sends_localized_message_and_rechecks_changed_roles(self):
        import config

        pending = SimpleNamespace(id=10, name=config.ROLE_TEMP)
        new = SimpleNamespace(id=config.ROLE_NEW_MEMBER_ID, name="New Member")
        english = member(1, [pending])
        french = member(2, [pending])
        changed = member(3, [pending])
        changed_now = member(3, [pending, new])

        async def fetch_members(**_kwargs):
            for person in (english, french, changed):
                yield person

        guild = SimpleNamespace(roles=[pending], get_role=lambda _id: new,
                                fetch_members=fetch_members,
                                fetch_member=AsyncMock(side_effect=lambda uid: {
                                    1: english, 2: french, 3: changed_now}[uid]))
        ctx = SimpleNamespace(author=SimpleNamespace(id=99), guild=guild, send=AsyncMock())
        cog = LessonReminder(SimpleNamespace(get_cog=Mock(return_value=object())))
        records = [{"user_id": "1", "lang": "en"}]
        store = SimpleNamespace(get_members=AsyncMock(return_value=records))
        with (patch("cogs.lesson_reminder.is_bot_admin", return_value=True),
              patch("cogs.lesson_reminder.store", store),
              patch("cogs.lesson_reminder.asyncio.sleep", new_callable=AsyncMock)):
            await cog.remind_c_lesson.callback(cog, ctx)

        self.assertIn("C language lesson", english.send.await_args.args[0])
        self.assertEqual(english.send.await_args.kwargs["view"].start.label, "Complete my details")
        self.assertIn("cours de langage C", french.send.await_args.args[0])
        changed_now.send.assert_not_awaited()
        self.assertIn("2 sent", ctx.send.await_args.args[0])
        self.assertIn("1 no longer eligible", ctx.send.await_args.args[0])

    async def test_non_admin_cannot_start_sweep(self):
        ctx = SimpleNamespace(author=SimpleNamespace(id=99), guild=object(), send=AsyncMock())
        cog = LessonReminder(SimpleNamespace())
        with patch("cogs.lesson_reminder.is_bot_admin", return_value=False):
            await cog.remind_c_lesson.callback(cog, ctx)
        self.assertIn("admins only", ctx.send.await_args.args[0])
