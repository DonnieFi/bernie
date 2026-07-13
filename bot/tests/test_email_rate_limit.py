"""Phase 34 — email send rate limits + #anvil alert."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from email_service import EmailRateLimitError, _check_rate_limit


class TestEmailRateLimit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = {
            "anvil_channel_id": "999",
            "email": {"max_sends_per_hour": 2, "max_sends_per_domain_per_hour": 1},
        }

    async def test_requester_cap_raises(self):
        mock_db = MagicMock()
        mock_db.count_email_sends_since = AsyncMock(return_value=2)

        with (
            patch("db_binding.get_database", return_value=mock_db),
            patch("email_service._notify_anvil_rate_limit", new_callable=AsyncMock) as anvil,
            patch("db_writes.routed", new_callable=AsyncMock) as routed,
        ):
            with self.assertRaises(EmailRateLimitError):
                await _check_rate_limit("dad", "dad@example.com", None, self.config)

        routed.assert_awaited()
        self.assertEqual(routed.await_args.args[0], "log_activity")
        self.assertEqual(routed.await_args.kwargs.get("event_type"), "email_rate_limited")
        anvil.assert_awaited_once()

    async def test_domain_cap_raises(self):
        mock_db = MagicMock()
        mock_db.count_email_sends_since = AsyncMock(side_effect=[0, 1])

        with (
            patch("db_binding.get_database", return_value=mock_db),
            patch("email_service._notify_anvil_rate_limit", new_callable=AsyncMock) as anvil,
            patch("db_writes.routed", new_callable=AsyncMock) as routed,
        ):
            with self.assertRaises(EmailRateLimitError) as ctx:
                await _check_rate_limit("dad", "kid@school.ca", None, self.config)

        self.assertIn("school.ca", str(ctx.exception))
        anvil.assert_awaited_once()
        routed.assert_awaited()
        self.assertEqual(routed.await_args.kwargs.get("event_type"), "email_rate_limited")

    async def test_under_limit_passes(self):
        mock_db = MagicMock()
        mock_db.count_email_sends_since = AsyncMock(return_value=0)

        with (
            patch("db_binding.get_database", return_value=mock_db),
            patch("db_writes.routed", new_callable=AsyncMock) as routed,
        ):
            await _check_rate_limit("dad", "dad@example.com", None, self.config)

        routed.assert_not_awaited()
