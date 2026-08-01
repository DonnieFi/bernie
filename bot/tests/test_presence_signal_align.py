"""Align update_presence last_home_signal with apply_presence_tick (agent2 review)."""
from __future__ import annotations

import os
import tempfile
import unittest

import database as db


class TestPresenceSignalAlign(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old = db.DB_PATH
        db.DB_PATH = self._tmp.name
        db._conn = db._async_conn = db._conn_path = None
        await db.init_db()

    async def asyncTearDown(self):
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old
        db._conn = None
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    async def test_update_presence_home_sets_last_home_signal(self):
        await db.update_presence("alice", True, "aa:bb", home_signal_ts=12345.0)
        signals = await db.get_last_home_signals(["alice"])
        self.assertEqual(signals["alice"], 12345.0)

    async def test_apply_presence_tick_still_sets_signal(self):
        res = await db.apply_presence_tick([
            {
                "person_id": "bob",
                "is_home": True,
                "device_mac": None,
                "set_last_home_signal": 999.0,
            }
        ])
        self.assertTrue(res[0][1])
        signals = await db.get_last_home_signals(["bob"])
        self.assertEqual(signals["bob"], 999.0)


if __name__ == "__main__":
    unittest.main()
