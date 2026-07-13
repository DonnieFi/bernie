"""c79.1–3: usage SQL aggregate, activity index migration, _db_read path."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database as db


class TestC79UsageAggregate(unittest.IsolatedAsyncioTestCase):
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

    async def test_get_daily_per_model_sql_group(self):
        await db.log_token_usage(
            model="claude-test", input_tokens=100, output_tokens=50,
            session_id="1-1000", surface="discord",
        )
        await db.log_token_usage(
            model="claude-test", input_tokens=10, output_tokens=5,
            session_id="1-1000", surface="discord",
        )
        rows = await db.get_daily_per_model(days=7)
        self.assertTrue(rows)
        match = [r for r in rows if r["model"] == "claude-test"]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["in_tok"], 110)
        self.assertEqual(match[0]["requests"], 2)

    async def test_top_sessions_batch_titles(self):
        await db.log_token_usage(
            model="m", input_tokens=1000, output_tokens=1,
            session_id="sid-a", surface="discord",
        )
        await db.cache_session_title("sid-a", "Cached Title A")
        with patch.object(db, "get_cached_session_title", new_callable=AsyncMock) as mock_one:
            top = await db.get_top_sessions(days=7, limit=5)
            mock_one.assert_not_called()  # c79.3: no per-session N+1 helper
        self.assertTrue(any(t["id"] == "sid-a" and t["title"] == "Cached Title A" for t in top))

    async def test_activity_index_exists(self):
        async with db._db_read() as conn:
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_activity_log_event_type_logged_at'"
            )
            self.assertIsNotNone(await cur.fetchone())

    async def test_db_read_bypasses_write_lock(self):
        """_db_read must not acquire the package write lock."""
        acquired = []
        real_lock = db._get_lock()

        class TrackingLock:
            async def __aenter__(self):
                acquired.append(True)
                return await real_lock.__aenter__()

            async def __aexit__(self, *a):
                return await real_lock.__aexit__(*a)

        with patch.object(db, "_get_lock", return_value=TrackingLock()):
            # force import path used by activity: database._db_read
            async with db._db_read() as conn:
                await conn.execute("SELECT 1")
        self.assertEqual(acquired, [], "_db_read must not take write lock")


if __name__ == "__main__":
    unittest.main()
