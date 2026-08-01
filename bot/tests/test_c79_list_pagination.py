"""c79.4 — list_all_tasks / automations pagination (default ≤100, hard max)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database as db


class TestClampListLimit(unittest.TestCase):
    def test_default(self):
        self.assertEqual(db.clamp_list_limit(None), 100)

    def test_ok(self):
        self.assertEqual(db.clamp_list_limit(50), 50)

    def test_over_hard_max(self):
        with self.assertRaises(ValueError) as ctx:
            db.clamp_list_limit(101)
        self.assertIn("hard max", str(ctx.exception))

    def test_offset_negative(self):
        with self.assertRaises(ValueError):
            db.clamp_list_offset(-1)


class TestListPaginationDB(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old = db.DB_PATH
        self._old_role = os.environ.get("ROLE")
        os.environ["ROLE"] = "monolith"
        db.DB_PATH = self._tmp.name
        await db.init_db()

    async def asyncTearDown(self):
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old
        if self._old_role is None:
            os.environ.pop("ROLE", None)
        else:
            os.environ["ROLE"] = self._old_role
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    async def test_list_all_tasks_respects_limit(self):
        for i in range(5):
            await db.create_task(
                title=f"t{i}",
                assigned_to="person:red",
                assigned_by="person:red",
            )
        page = await db.list_all_tasks(limit=2, offset=0)
        self.assertEqual(len(page), 2)
        page2 = await db.list_all_tasks(limit=2, offset=2)
        self.assertEqual(len(page2), 2)
        ids = {r["id"] for r in page} | {r["id"] for r in page2}
        self.assertEqual(len(ids), 4)

    async def test_list_all_tasks_rejects_over_max(self):
        with self.assertRaises(ValueError):
            await db.list_all_tasks(limit=101)


if __name__ == "__main__":
    unittest.main()
