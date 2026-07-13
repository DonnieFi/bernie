"""family-bot-ah5.3: batch unsent-reminder + person pref lookups."""
from __future__ import annotations

import os
import tempfile
import unittest

import database as db


class TestRemindersBatch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old = db.DB_PATH
        db.DB_PATH = self._tmp.name
        db._conn = None
        db._async_conn = None
        db._conn_path = None
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

    async def test_filter_unsent_reminders(self):
        await db.mark_reminder_sent("e1", 15)
        await db.mark_reminder_sent("e2", 30)
        unsent = await db.filter_unsent_reminders(
            [("e1", 15), ("e1", 30), ("e2", 30), ("e3", 15)]
        )
        self.assertEqual(unsent, {("e1", 30), ("e3", 15)})

    async def test_get_person_prefs_by_discord_ids(self):
        await db.set_person_pref("a", discord_id=111, reminders_enabled=False, dm_mode=True)
        await db.set_person_pref("b", discord_id=222, reminders_enabled=True, dm_mode=False)
        prefs = await db.get_person_prefs_by_discord_ids([111, 222, 333])
        self.assertFalse(prefs[111]["reminders_enabled"])
        self.assertFalse(prefs[222]["dm_mode"])
        self.assertTrue(prefs[333]["reminders_enabled"])  # default


if __name__ == "__main__":
    unittest.main()
