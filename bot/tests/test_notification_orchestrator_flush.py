import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class NotificationRouterFlushTests(unittest.TestCase):
    def test_flush_pending_noops_when_queue_empty(self):
        from notification_router import NotificationRouter

        router = NotificationRouter(MagicMock())

        async def _run():
            # Reads still go through get_database(); mock that path
            with patch(
                "notification_router.get_database",
            ) as gd:
                db = MagicMock()
                db.list_pending_notifications = AsyncMock(return_value=[])
                gd.return_value = db
                await router.flush_pending("123456789")
                db.list_pending_notifications.assert_called_once_with("123456789")

        asyncio.run(_run())

    def test_notify_and_flush_both_invoke_db_layer(self):
        from notification_router import Notification, NotificationRouter

        router = NotificationRouter(MagicMock())
        router._send_discord = AsyncMock(return_value=True)
        router._send_email = AsyncMock(return_value=False)

        async def _run():
            db = MagicMock()
            db.list_pending_notifications = AsyncMock(return_value=[])
            with patch("notification_router.db_writes.routed", new_callable=AsyncMock) as routed, \
                 patch("notification_router.get_database", return_value=db):
                await router.notify(
                    Notification(recipient_id="123", message="test", urgency="high")
                )
                await router.flush_pending("123")
            # high urgency notify logs via routed
            self.assertTrue(routed.await_count >= 1)
            db.list_pending_notifications.assert_called_once_with("123")

        asyncio.run(_run())
