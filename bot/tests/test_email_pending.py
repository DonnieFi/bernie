"""Phase 34 — kid email pending expiry."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database as db
from db_binding import bind_database
from email_pending_delivery import run_email_pending_expiry_sweep


class TestEmailPendingExpiry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "email_pending_test.db")
        await db.init_db()
        bind_database(db)

    async def asyncTearDown(self):
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_expire_stale_pending(self):
        old = (
            datetime.now(dt_timezone.utc) - timedelta(hours=30)
        ).isoformat().replace("+00:00", "Z")
        pid = await db.create_email_pending(
            recipient="dad@example.com",
            subject="Hi",
            body="Test",
            requester_id="child1",
            requester_role="kids",
        )
        async with db._db_conn() as conn:
            await conn.execute(
                "UPDATE email_pending SET created_at = ?, smithy_message_id = ? WHERE id = ?",
                (old, "55555", pid),
            )
            await conn.commit()

        mock_msg = MagicMock()
        mock_msg.content = "draft"
        mock_msg.edit = AsyncMock()
        mock_channel = MagicMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_msg)
        mock_bot = MagicMock()
        mock_bot.get_channel = MagicMock(return_value=mock_channel)

        config = {"schedule_channel_id": "123", "email": {}}
        count = await run_email_pending_expiry_sweep(config, mock_bot)

        self.assertEqual(count, 1)
        row = await db.get_email_pending(pid)
        self.assertEqual(row["status"], "expired")
        mock_msg.edit.assert_awaited_once()
        edit_arg = mock_msg.edit.call_args.kwargs.get("content") or mock_msg.edit.call_args.args[0]
        self.assertIn("Expired", edit_arg)