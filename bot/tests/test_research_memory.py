import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database as test_db


class TestResearchMemory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old = test_db.DB_PATH
        test_db.DB_PATH = self._tmp.name
        await test_db.init_db()
        await test_db.ensure_pending_hitl_schema()

    async def asyncTearDown(self):
        test_db.DB_PATH = self._old
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    async def test_append_and_list_round_trip(self):
        await test_db.append_research_memory(42, "finding", "First hotel option.")
        await test_db.append_research_memory(42, "preference", "Prefer waterfront.")
        entries = await test_db.list_research_memory(42)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["kind"], "finding")
        self.assertEqual(entries[1]["content"], "Prefer waterfront.")

    async def test_format_for_prompt_includes_prior(self):
        await test_db.append_research_memory(7, "finding", "Cape Breton lodges.")
        block = test_db.format_research_memory_for_prompt(await test_db.list_research_memory(7))
        self.assertIn("Cape Breton", block)
        self.assertIn("Prior research", block)


if __name__ == "__main__":
    unittest.main()
