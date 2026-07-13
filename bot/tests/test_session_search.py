"""5hy.11 + 8lx.4 — conversation_history FTS5 session_search."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database as db


class TestConversationHistoryFts(unittest.IsolatedAsyncioTestCase):
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

    async def test_greenfield_has_fts_table(self):
        async with db._db_conn() as conn:
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_history_fts'"
            )
            self.assertIsNotNone(await cur.fetchone())

    async def test_discover_finds_content(self):
        await db.add_message(channel_id=1, role="user", content="Child1 has a piano recital Friday")
        await db.add_message(channel_id=1, role="assistant", content="Noted the recital.")
        hits = await db.search_conversation_history("piano recital")
        self.assertTrue(hits)
        self.assertTrue(any("piano" in (h["content"] or "").lower() for h in hits))

    async def test_scroll_and_browse(self):
        for i in range(5):
            await db.add_message(channel_id=42, role="user", content=f"msg {i} soccer practice")
        page = await db.scroll_conversation_history(channel_id=42, limit=3)
        self.assertEqual(len(page), 3)
        mid = page[1]["id"]
        window = await db.browse_conversation_history(around_id=mid, limit=4)
        self.assertTrue(any(r["id"] == mid for r in window))

    async def test_brownfield_ensure_idempotent(self):
        """Brownfield ensure_* can re-run safely (8lx.4 dual-path)."""
        await db.ensure_conversation_history_fts()
        await db.ensure_conversation_history_fts()
        hits = await db.search_conversation_history("xyzzynonexistenttoken")
        self.assertEqual(hits, [])


class TestSessionSearchMigrationVersion(unittest.IsolatedAsyncioTestCase):
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

    async def test_migration_v8_recorded(self):
        applied = await db.get_applied_schema_migration_versions()
        self.assertIn(8, applied)
        self.assertIn(9, applied)


if __name__ == "__main__":
    unittest.main()
