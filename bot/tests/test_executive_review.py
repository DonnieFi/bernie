import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from executive_review import review_deliverable
from typed_outputs import DeliverableMeta, ResearchDeliverable


class TestExecutiveReview(unittest.IsolatedAsyncioTestCase):
    async def test_returns_none_without_audit_model(self):
        d = ResearchDeliverable(topic="t", content="body")
        out = await review_deliverable(d, config={}, container=MagicMock())
        self.assertIsNone(out)

    async def test_review_sets_reviewed_status(self):
        d = ResearchDeliverable(topic="t", content="body")
        reviewed = ResearchDeliverable(
            topic="t",
            content="body",
            meta=DeliverableMeta(confidence=0.8, draft_status="reviewed"),
        )
        mock_result = MagicMock()
        mock_result.output = reviewed
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_result)

        with patch("agent_utils.make_typed_agent", return_value=mock_agent):
            out = await review_deliverable(
                d,
                config={"audit_model": "claude-haiku-4-5-20251001"},
                container=MagicMock(),
            )
        self.assertIsNotNone(out)
        self.assertEqual(out.meta.draft_status, "reviewed")


if __name__ == "__main__":
    unittest.main()
