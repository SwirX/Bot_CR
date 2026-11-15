import unittest
from types import SimpleNamespace

from cogs._ui import MemberPickerView


class MemberSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_by_name_and_id_preserves_selections(self):
        people = [SimpleNamespace(id=1, display_name="Alice", name="alice123", roles=[], bot=False),
                  SimpleNamespace(id=2, display_name="Bob", name="robert", roles=[], bot=False)]
        picker = MemberPickerView(people, on_confirm=None, user=9)
        picker._chosen.add("1")
        self.assertTrue(picker.filter_members("robert"))
        self.assertEqual([p.id for p in picker._page_members()], [2])
        picker._chosen.add("2")
        self.assertTrue(picker.filter_members("1"))
        self.assertEqual([p.id for p in picker.selected()], [1, 2])
        self.assertFalse(picker.filter_members("missing"))
        self.assertEqual([p.id for p in picker._page_members()], [1])
        self.assertTrue(picker.filter_members(""))
        self.assertEqual(len(picker._page_members()), 2)
