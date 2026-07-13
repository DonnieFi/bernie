import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cognitive_handlers.handlers.research_deliver import handle_research_deliver
from research_executive_delivery import ResearchDeliveryPlan


def _plan(*, route: str, content: str = "Findings about hotels.", urgency: str = "normal"):
    deliverable = MagicMock()
    deliverable.content = content
    deliverable.topic = "hotels"
    return ResearchDeliveryPlan(
        should_deliver=route not in ("ignore", "remember"),
        route=route,
        body_text=content,
        prefix="",
        urgency=urgency,
        fallback=False,
        topic="hotels",
        deliverable=deliverable,
    )


class TestResearchDeliverExecutive(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = MagicMock()
        self.db.get_cognitive_task = AsyncMock(return_value=None)
        self.db.get_task_output_by_key = AsyncMock(
            return_value={"content": "Findings about hotels."}
        )
        self.db.log_activity = AsyncMock()
        self.container = MagicMock()
        self.container.db = self.db
        router = MagicMock()
        router.notification = MagicMock(side_effect=lambda **kw: kw)
        router.notify = AsyncMock(return_value={"discord": True})
        self.container.notification_orchestrator = router

    async def test_low_confidence_not_delivered(self):
        with patch(
            "cognitive_handlers.handlers.research_deliver.prepare_research_for_delivery",
            AsyncMock(return_value=_plan(route="ignore")),
        ):
            result = await handle_research_deliver(
                {
                    "payload": {
                        "task_id": 1,
                        "requester_id": "123",
                        "topic": "hotels",
                        "delivery": "dm",
                    }
                },
                self.container,
            )
        self.assertFalse(result["_result"]["delivered"])
        self.assertEqual(result["_result"]["reason"], "ignored")
        self.container.notification_orchestrator.notify.assert_not_awaited()

    async def test_interrupt_delivers_with_high_urgency(self):
        with patch(
            "cognitive_handlers.handlers.research_deliver.prepare_research_for_delivery",
            AsyncMock(return_value=_plan(route="interrupt", urgency="high")),
        ), patch(
            "cognitive_handlers.handlers.research_deliver.send_email_via_gateway",
            AsyncMock(return_value="ok"),
        ), patch(
            "cognitive_handlers.handlers.research_deliver.deliver_discord_dm",
            AsyncMock(return_value=True),
        ):
            result = await handle_research_deliver(
                {
                    "payload": {
                        "task_id": 1,
                        "requester_id": "123",
                        "topic": "hotels",
                        "delivery": "dm",
                    }
                },
                self.container,
            )
        self.assertTrue(result["_result"]["delivered"])
        call_kw = self.container.notification_orchestrator.notification.call_args.kwargs
        self.assertEqual(call_kw.get("urgency"), "high")


if __name__ == "__main__":
    unittest.main()
