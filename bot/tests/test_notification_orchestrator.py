import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class NotificationRouterInterfaceTests(unittest.TestCase):
    def test_router_exposes_caller_interface(self):
        from notification_router import NotificationRouter

        router = NotificationRouter(MagicMock())
        for name in ("notify", "flush_pending", "ping", "notify_all", "notification"):
            self.assertTrue(hasattr(router, name), f"missing {name}")

    def test_notification_factory_builds_dataclass(self):
        from notification_router import NotificationRouter

        router = NotificationRouter(MagicMock())
        note = router.notification(recipient_id="123", message="hello")
        self.assertEqual(note.recipient_id, "123")
        self.assertEqual(note.message, "hello")

    def test_notify_delivers_high_urgency(self):
        from notification_router import Notification, NotificationRouter

        router = NotificationRouter(MagicMock())
        router._send_discord = AsyncMock(return_value=True)
        router._send_email = AsyncMock(return_value=False)

        async def _run():
            # Production uses db_writes.routed("log_notification", ...), not notification_router.db
            with patch("notification_router.db_writes.routed", new_callable=AsyncMock):
                result = await router.notify(
                    Notification(recipient_id="123", message="hello", urgency="high")
                )
            self.assertTrue(result.get("discord"))
            router._send_discord.assert_called_once()

        asyncio.run(_run())