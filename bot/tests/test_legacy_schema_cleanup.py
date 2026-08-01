"""40B-1f: brownfield migration user_preferences → person_preferences."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import sqlite_async
import sqlite3

try:
    import sqlite_async
    import database as db
except ModuleNotFoundError:
    db = None


@unittest.skipUnless(db is not None, "database not available")
class TestLegacySchemaCleanup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "legacy_cleanup.db")
        import sqlite3
        conn = sqlite3.connect(db.DB_PATH)
        conn.executescript("""
                CREATE TABLE conversation_history (
                    id INTEGER PRIMARY KEY, channel_id INTEGER, role TEXT,
                    content TEXT, created_at TEXT
                );
                CREATE TABLE person_preferences (
                    person_id TEXT PRIMARY KEY,
                    discord_id INTEGER,
                    reminders_enabled BOOLEAN NOT NULL DEFAULT 1,
                    dm_mode BOOLEAN NOT NULL DEFAULT 1,
                    reminder_minutes INTEGER NOT NULL DEFAULT 30,
                    preferred_channels TEXT DEFAULT 'discord',
                    quiet_hours_start TEXT,
                    quiet_hours_end TEXT,
                    updated_at TEXT
                );
                CREATE TABLE user_preferences (
                    discord_id INTEGER PRIMARY KEY,
                    name TEXT,
                    reminders INTEGER NOT NULL DEFAULT 1,
                    dm_mode INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE unified_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL DEFAULT 'chore',
                    status TEXT NOT NULL DEFAULT 'todo',
                    title TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    assigned_by TEXT NOT NULL,
                    acceptable_assignees TEXT NOT NULL DEFAULT '[]',
                    visibility TEXT NOT NULL DEFAULT 'family',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    is_recurring INTEGER NOT NULL DEFAULT 0,
                    snooze_count INTEGER NOT NULL DEFAULT 0,
                    remind_visibility TEXT NOT NULL DEFAULT 'private',
                    requires_approval INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    assigned_to TEXT NOT NULL,
                    assigned_by TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    in_progress INTEGER NOT NULL DEFAULT 0,
                    priority TEXT NOT NULL DEFAULT 'normal',
                    requires_approval INTEGER NOT NULL DEFAULT 0,
                    is_recurring INTEGER NOT NULL DEFAULT 0,
                    snooze_count INTEGER NOT NULL DEFAULT 0,
                    remind_visibility TEXT NOT NULL DEFAULT 'private',
                    created_at TEXT NOT NULL
                );
                INSERT INTO user_preferences (discord_id, name, reminders, dm_mode, updated_at)
                VALUES (111111111111111111, 'Dad', 1, 1, '2026-04-22T10:01:04Z');
                INSERT INTO tasks (id, title, assigned_to, assigned_by, created_at)
                VALUES (1, 'legacy chore', 'dad', 'dad', '2026-01-01T00:00:00Z');
                INSERT INTO unified_tasks (id, title, assigned_by, payload, created_at)
                VALUES (1, 'migrated', 'dad', '{"migrated_from_task_id": 1}', '2026-01-01T00:00:00Z');
        """)
        conn.commit()
        conn.close()

    async def asyncTearDown(self):
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_migrates_user_preferences_and_drops_legacy_tables(self):
        await db.init_db()
        prefs = await db.get_person_pref(discord_id=111111111111111111)
        self.assertTrue(prefs["reminders_enabled"])
        self.assertTrue(prefs["dm_mode"])
        async with db._db_conn() as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('user_preferences', 'family_prefs', 'tasks')"
            ) as cur:
                remaining = {r[0] for r in await cur.fetchall()}
        self.assertEqual(remaining, set())
