import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("APPWRITE_API_KEY", "y")

from cogs.details_reminder import DetailsReminder, needs_reminder  # noqa: E402

EXPECTED_MESSAGE = (
    "Salam! Xfnak bli ba9i ma3titinax smiya dyalk kamla, clicki 3la botona li l7t "
    "bax tdkhlha, ghadi n3tiwk wa7d smiya cursive o mnha n9dro ndiro lik verification "
    "fl server, t9dr tgul 7ta wax dri ula bnt o 3idmilad dyalk bax nsifto msg fl server "
    "mnin ywsl bax nbarko lik\n\n\n"
    "سلام! شفناك بلي باقي ما عطيتيناش سميتك كاملة، كليكي على bouton اللي لتحت "
    "باش تدخلها، غادي نعطيوك واحد السمية cursive ومنها نقدرو نديرو ليك verification "
    "فال serveur، تقدر تقول حتى واش دري ولا بنت وعيد ميلاد ديالك باش نصيفطو message فال serveur "
    "منين يوصل باش نباركو ليك"
)


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

    async def test_sends_exact_message_and_rechecks_changed_roles(self):
        import config

        pending = SimpleNamespace(id=10, name=config.ROLE_TEMP)
        new = SimpleNamespace(id=config.ROLE_NEW_MEMBER_ID, name="New Member")
        first = member(1, [pending])
        second = member(2, [pending])
        changed = member(3, [pending])
        changed_now = member(3, [pending, new])

        async def fetch_members(**_kwargs):
            for person in (first, second, changed):
                yield person

        guild = SimpleNamespace(roles=[pending], get_role=lambda _id: new,
                                fetch_members=fetch_members,
                                fetch_member=AsyncMock(side_effect=lambda uid: {
                                    1: first, 2: second, 3: changed_now}[uid]))
        ctx = SimpleNamespace(author=SimpleNamespace(id=99), guild=guild, send=AsyncMock())
        cog = DetailsReminder(SimpleNamespace(get_cog=Mock(return_value=object())))
        with (patch("cogs.details_reminder.is_bot_admin", return_value=True),
              patch("cogs.details_reminder.asyncio.sleep", new_callable=AsyncMock)):
            await cog.remind_details.callback(cog, ctx)

        self.assertEqual(first.send.await_args.args[0], EXPECTED_MESSAGE)
        self.assertEqual(second.send.await_args.args[0], EXPECTED_MESSAGE)
        button = first.send.await_args.kwargs["view"].start
        self.assertEqual(button.custom_id, "onboarding_start")
        self.assertEqual(button.label, "Dkhel ma3loumatk")
        changed_now.send.assert_not_awaited()
        self.assertIn("2 sent", ctx.send.await_args.args[0])
        self.assertIn("1 no longer eligible", ctx.send.await_args.args[0])

    async def test_non_admin_cannot_start_sweep(self):
        ctx = SimpleNamespace(author=SimpleNamespace(id=99), guild=object(), send=AsyncMock())
        cog = DetailsReminder(SimpleNamespace())
        with patch("cogs.details_reminder.is_bot_admin", return_value=False):
            await cog.remind_details.callback(cog, ctx)
        self.assertIn("admins only", ctx.send.await_args.args[0])
