"""Tests for TransitTrackingManager session lifecycle."""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import MagicMock

from transit_tracking import (
    TransitTrackingManager,
    TransitSession,
    _fallback_message_text,
    error_retry_seconds,
)


class TestTransitTrackingManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mgr = TransitTrackingManager()

    async def test_stop_session_missing(self):
        self.assertFalse(await self.mgr.stop_session(999001))

    async def test_stop_session_cancels_task(self):
        sess = TransitSession(
            user_id=1,
            channel_id=2,
            person_id="child1",
            person_display="Child1",
            vehicle_id="3160",
            route_id="4",
            landmark_key="home",
        )

        async def _cancelled_task():
            raise asyncio.CancelledError

        sess.task = asyncio.create_task(_cancelled_task())
        self.mgr._sessions[1] = sess

        self.assertTrue(await self.mgr.stop_session(1))
        self.assertNotIn(1, self.mgr._sessions)

    def test_fallback_message_from_embed(self):
        embed = MagicMock()
        embed.title = "Tracking bus 3160 · update 2"
        embed.description = "**Vehicle 3160** · ~420m straight-line"
        embed.fields = [MagicMock(name="Google Maps", value="[Open full map](https://maps.google.com)")]
        text = _fallback_message_text(None, embed)
        self.assertIn("3160", text)
        self.assertIn("420m", text)
        self.assertIn("Google Maps", text)

    def test_fallback_message_without_content_or_embed(self):
        text = _fallback_message_text(None, None)
        self.assertIn("/bus", text)

    def test_error_retry_capped_by_poll_interval(self):
        self.assertLessEqual(error_retry_seconds(), 180)

    def test_set_and_get_binding_expires(self):
        self.mgr.set_vehicle_binding("child1", "99", "4")
        b = self.mgr.get_vehicle_binding("child1")
        self.assertIsNotNone(b)
        self.assertEqual(b.vehicle_id, "99")
        b.expires_at = time.monotonic() - 1
        self.assertIsNone(self.mgr.get_vehicle_binding("child1"))


if __name__ == "__main__":
    unittest.main()
