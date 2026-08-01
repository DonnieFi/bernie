"""Phase 34 — worker send blocked before Gmail API."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from email_service import EmailPolicyError, send


class TestEmailWorkerBlock(unittest.IsolatedAsyncioTestCase):
    async def test_non_family_blocked_before_gmail(self):
        config = {
            "family_members": {
                "Dad": {"email": "dad@example.com", "role": "admin"},
            }
        }
        mock_db = MagicMock()
        mock_db.count_email_sends_since = AsyncMock(return_value=0)

        with (
            patch("db_binding.get_database", return_value=mock_db),
            patch("email_service._run_sync", new_callable=AsyncMock) as run_sync,
        ):
            with self.assertRaises(EmailPolicyError) as ctx:
                await send(
                    "coach@school.ca",
                    "Subject",
                    "Body",
                    requester_id="agent:study-guide",
                    requester_role="system",
                    config=config,
                )

        self.assertIn("coach@school.ca", str(ctx.exception))
        run_sync.assert_not_awaited()