"""Phase 34 — kid email pending claim-before-send."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database as db


class TestEmailPendingClaim(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "email_claim_test.db")
        await db.init_db()

    async def asyncTearDown(self):
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_only_one_claim_succeeds(self):
        pid = await db.create_email_pending(
            recipient="dad@example.com",
            subject="Hi",
            body="Test",
            requester_id="child1",
            requester_role="kids",
            reply_to_gmail_id="gid1",
            thread_id="tid1",
        )
        first = await db.claim_email_pending_for_send(pid, decided_by="parent1")
        second = await db.claim_email_pending_for_send(pid, decided_by="parent2")
        self.assertTrue(first)
        self.assertFalse(second)
        row = await db.get_email_pending(pid)
        self.assertEqual(row["status"], "sending")
        self.assertEqual(row["reply_to_gmail_id"], "gid1")
        self.assertEqual(row["thread_id"], "tid1")