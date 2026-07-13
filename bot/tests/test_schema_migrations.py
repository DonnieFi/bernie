"""40B-2B: brownfield init_db runs versioned schema migrations."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import sqlite3

try:
    import database as db
except ModuleNotFoundError:
    db = None


@unittest.skipUnless(db is not None, "database not available")
class TestSchemaMigrationsBrownfield(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "brownfield.db")
        conn = sqlite3.connect(db.DB_PATH)
        conn.executescript(
            """
            CREATE TABLE conversation_history (
                id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
        conn.close()

    async def asyncTearDown(self):
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_init_db_applies_migrations_and_records_versions(self):
        await db.init_db()
        conn = sqlite3.connect(db.DB_PATH)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='email_signals'"
            )
            self.assertIsNotNone(cur.fetchone())
            cur = conn.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        self.assertGreaterEqual(len(rows), 9)
        versions = [int(r[0]) for r in rows]
        self.assertIn(1, versions)
        self.assertIn(9, versions)

    async def test_second_init_db_is_idempotent(self):
        await db.init_db()
        await db.init_db()
        conn = sqlite3.connect(db.DB_PATH)
        try:
            count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 9)

    async def test_record_schema_migration_is_idempotent(self):
        await db.ensure_schema_migrations_table()
        now = "2026-07-07T00:00:00Z"
        await db.record_schema_migration(99, "test_gate_migration", now)
        await db.record_schema_migration(99, "test_gate_migration", now)
        conn = sqlite3.connect(db.DB_PATH)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 99"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)


@unittest.skipUnless(db is not None, "database not available")
class TestSchemaMigrationsGreenfield(unittest.IsolatedAsyncioTestCase):
    """family-bot-1hs: empty DB init_db records all MIGRATION_SPECS versions on first boot."""

    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "greenfield.db")
        # Empty file — no conversation_history yet
        open(db.DB_PATH, "a").close()

    async def asyncTearDown(self):
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_greenfield_init_db_records_all_migration_versions(self):
        await db.init_db()
        conn = sqlite3.connect(db.DB_PATH)
        try:
            rows = conn.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            conn.close()
        versions = [int(r[0]) for r in rows]
        self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(len(rows), 9)

    async def test_greenfield_second_init_is_idempotent(self):
        await db.init_db()
        await db.init_db()
        conn = sqlite3.connect(db.DB_PATH)
        try:
            count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 9)
