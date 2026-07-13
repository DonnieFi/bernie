import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestNotifyTools(unittest.IsolatedAsyncioTestCase):
    async def test_notify_family_member_uses_router_factory(self):
        from tools.notify import handle_notify_family_member

        orchestrator = MagicMock()
        note = MagicMock()
        orchestrator.notification = MagicMock(return_value=note)
        orchestrator.notify = AsyncMock()

        class Ctx:
            shadow = False
            person_id = "person:red"
            group = "parents"
            config = {}

            class services:
                pass

        ctx = Ctx()
        ctx.services.orchestrator = orchestrator

        mock_registry = MagicMock()
        mock_registry.resolve.return_value = "person:red"
        mock_registry.get.return_value = {"discord_id": "12345678901234567"}
        with patch("constants.registry", mock_registry):
            res = await handle_notify_family_member(
                {"recipient": "dad", "message": "Dinner in 10", "urgency": "normal"},
                ctx,
            )

        self.assertIn("Notification sent", res)
        orchestrator.notification.assert_called_once_with(
            recipient_id="12345678901234567",
            message="Dinner in 10",
            urgency="normal",
        )
        orchestrator.notify.assert_awaited_once_with(note)

    async def test_notify_family_member_shadow_skips_router(self):
        from tools.notify import handle_notify_family_member

        orchestrator = MagicMock()
        orchestrator.notification = MagicMock()
        orchestrator.notify = AsyncMock()

        class Ctx:
            shadow = True
            person_id = "person:red"
            group = "parents"
            config = {}

            class services:
                pass

        ctx = Ctx()
        ctx.services.orchestrator = orchestrator
        res = await handle_notify_family_member(
            {"recipient": "dad", "message": "Quiet test"},
            ctx,
        )
        self.assertIn("[shadow:", res)
        orchestrator.notification.assert_not_called()
        orchestrator.notify.assert_not_awaited()
