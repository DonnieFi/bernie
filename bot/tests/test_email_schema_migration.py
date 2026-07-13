"""Brownfield DB: init_db must create email tables on existing production DBs."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

import sqlite3

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database as db
from db_binding import bind_database

_EMAIL_TABLES = sorted(
    [
        "email_ingest_cursor",
        "email_pending",
        "email_send_rate",
        "email_signals",
    ]
)


class TestEmailSchemaMigration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "brownfield.db")
        if hasattr(db, "close_db"):
            await db.close_db()
        # Simulate production: conversation_history exists, email tables do not.
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute(
            """CREATE TABLE conversation_history (
                   id INTEGER PRIMARY KEY, channel_id INTEGER, role TEXT,
                   content TEXT, created_at TEXT)"""
        )
        conn.commit()
        conn.close()
        bind_database(db)

    async def asyncTearDown(self):
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_init_db_creates_email_tables_on_existing_db(self):
        await db.init_db()
        async with db._db_conn() as conn:
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'email_%'"
            )
            names = sorted(r[0] for r in await cur.fetchall())
        self.assertEqual(names, _EMAIL_TABLES)

    async def test_ensure_email_schema_idempotent(self):
        await db.ensure_email_schema()
        await db.ensure_email_schema()
        async with db._db_conn() as conn:
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'email_%'"
            )
            names = sorted(r[0] for r in await cur.fetchall())
        self.assertEqual(names, _EMAIL_TABLES)