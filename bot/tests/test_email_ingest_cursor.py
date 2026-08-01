"""Phase 34 — ingest cursor must not advance on failure."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cognitive_workers.inbox_ingest import run_inbox_ingest


class TestIngestCursor(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_not_advanced_on_get_message_failure(self):
        mock_db = MagicMock()
        mock_db.email_schema_ready = AsyncMock(return_value=True)
        mock_db.get_email_ingest_history_id = AsyncMock(return_value=None)
        mock_db.get_email_signal_by_gmail_id = AsyncMock(return_value=None)
        mock_identity = MagicMock()

        with (
            patch("cognitive_workers.inbox_ingest.gmail_is_configured", return_value=True),
            patch(
                "cognitive_workers.inbox_ingest.list_recent",
                new_callable=AsyncMock,
                return_value={"messages": [{"id": "g1"}, {"id": "g2"}]},
            ),
            patch(
                "cognitive_workers.inbox_ingest.get_message",
                new_callable=AsyncMock,
            ) as get_msg,
            patch(
                "cognitive_workers.inbox_ingest._summarize",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("cognitive_workers.inbox_ingest.family_email_set", return_value=set()),
            patch("cognitive_workers.inbox_ingest._person_for_email", return_value="dad"),
            patch("db_writes.routed", new_callable=AsyncMock) as routed,
        ):
            get_msg.side_effect = [
                {
                    "gmail_id": "g1",
                    "headers": {},
                    "body_text": "",
                    "subject": "A",
                    "thread_id": "t",
                    "received_at": "2026-01-01T00:00:00Z",
                    "from_header": "",
                    "delivered_to_header": "",
                },
                Exception("api down"),
            ]
            count = await run_inbox_ingest({"family_members": {}}, mock_db, mock_identity)

        self.assertEqual(count, 1)
        # Cursor only advances when batch_complete; failure of g2 aborts batch.
        cursor_ops = [c for c in routed.await_args_list if c.args and c.args[0] == "set_email_ingest_history_id"]
        self.assertEqual(cursor_ops, [])
