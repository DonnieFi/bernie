"""Phase 34 — inbox ingest parsing and dedup."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cognitive_workers.inbox_ingest import parse_forward_metadata, run_inbox_ingest


class TestParseForwardMetadata(unittest.TestCase):
    def setUp(self):
        self.family = {"dad@example.com", "kid@school.ca"}

    def test_delivered_to_family_forwarder(self):
        meta = parse_forward_metadata(
            {"from": "Coach <coach@school.ca>", "delivered-to": "dad@example.com"},
            "",
            self.family,
        )
        self.assertEqual(meta["forwarder_email"], "dad@example.com")
        self.assertEqual(meta["sender_email"], "coach@school.ca")

    def test_forward_marker_in_body(self):
        body = (
            "---------- Forwarded message ---------\n"
            "From: Dad <dad@example.com>\n"
            "Subject: Test\n"
        )
        meta = parse_forward_metadata(
            {"from": "Coach <coach@school.ca>"},
            body,
            self.family,
        )
        self.assertEqual(meta["forwarder_email"], "dad@example.com")


class TestInboxIngestDedup(unittest.IsolatedAsyncioTestCase):
    async def test_skips_existing_gmail_id(self):
        mock_db = MagicMock()
        mock_db.email_schema_ready = AsyncMock(return_value=True)
        mock_db.get_email_ingest_history_id = AsyncMock(return_value=None)
        mock_db.get_email_signal_by_gmail_id = AsyncMock(return_value={"gmail_id": "g1"})
        mock_identity = MagicMock()

        with (
            patch("cognitive_workers.inbox_ingest.gmail_is_configured", return_value=True),
            patch(
                "cognitive_workers.inbox_ingest.list_recent",
                new_callable=AsyncMock,
                return_value={"messages": [{"id": "g1"}]},
            ),
            patch(
                "email_service.get_profile_history_id",
                new_callable=AsyncMock,
                return_value="hid99",
            ),
            patch("cognitive_workers.inbox_ingest.get_message", new_callable=AsyncMock) as get_msg,
            # Writes go through db_writes.routed, not mock_db methods
            patch("db_writes.routed", new_callable=AsyncMock),
        ):
            count = await run_inbox_ingest({"family_members": {}}, mock_db, mock_identity)

        self.assertEqual(count, 0)
        get_msg.assert_not_awaited()