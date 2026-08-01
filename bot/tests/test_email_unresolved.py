"""Phase 34 — unknown ingest sender logs unresolved entity."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cognitive_workers.inbox_ingest import _ingest_message_id


class TestEmailUnresolved(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_sender_logs_unresolved(self):
        mock_db = MagicMock()
        mock_db.get_email_signal_by_gmail_id = AsyncMock(return_value=None)
        mock_identity = MagicMock()
        mock_identity.log_unresolved_entity = AsyncMock()

        parsed = {
            "gmail_id": "g99",
            "thread_id": "t1",
            "received_at": "2026-01-01T00:00:00Z",
            "subject": "Field trip",
            "from_header": "Coach <coach@school.ca>",
            "delivered_to_header": "dad@example.com",
            "body_text": "",
            "headers": {"from": "Coach <coach@school.ca>", "delivered-to": "dad@example.com"},
        }

        with (
            patch("cognitive_workers.inbox_ingest.get_message", new_callable=AsyncMock, return_value=parsed),
            patch("cognitive_workers.inbox_ingest._summarize", new_callable=AsyncMock, return_value=None),
            patch("cognitive_workers.inbox_ingest.family_email_set", return_value={"dad@example.com"}),
            patch("cognitive_workers.inbox_ingest._person_for_email", return_value=None),
            patch("db_writes.routed", new_callable=AsyncMock) as routed,
        ):
            outcome = await _ingest_message_id("g99", {"family_members": {}}, mock_db, mock_identity)

        self.assertEqual(outcome, "ingested")
        mock_identity.log_unresolved_entity.assert_awaited_once()
        ops = [c.args[0] for c in routed.await_args_list]
        self.assertIn("log_activity", ops)
        self.assertIn("insert_email_signal", ops)
