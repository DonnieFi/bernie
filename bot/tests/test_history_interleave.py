"""family-bot-5oh: interleaved get_history + add_message (SPEC Appendix A)."""

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import database as db
except ModuleNotFoundError:
    db = None


@unittest.skipUnless(db is not None, "database not available")
class TestHistoryInterleave(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "hist.db")
        await db.init_db()

    async def asyncTearDown(self):
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old
        self._tmpdir.cleanup()

    async def test_interleaved_write_and_read(self):
        channel_id = 42

        async def writer():
            for i in range(20):
                await db.add_message(channel_id, "user", f"msg-{i}")

        async def reader():
            rows = []
            for _ in range(20):
                rows = await db.get_history(channel_id, limit=50)
                await asyncio.sleep(0)
            return rows

        _, history = await asyncio.gather(writer(), reader())
        final = await db.get_history(channel_id, limit=50)
        self.assertEqual(len(final), 20)
        self.assertEqual(final[0]["content"], "msg-0")
        self.assertEqual(final[-1]["content"], "msg-19")
        # reader completed without hang; last snapshot is a list (may be partial mid-run)
        self.assertIsInstance(history, list)
