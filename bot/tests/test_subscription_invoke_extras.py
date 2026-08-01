"""Subscription invoke: timeout and structured output wiring."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pydantic import BaseModel


class _JudgeOut(BaseModel):
    score: int


class TestSubscriptionInvokeExtras(unittest.IsolatedAsyncioTestCase):
    async def test_complete_text_forwards_timeout(self):
        captured: list[object] = []

        async def _chain(req, cfg, **kw):
            captured.append(req)
            from completion_router import CompletionResult
            return CompletionResult(provider="grok", model="grok-4.5", text="ok"), []

        with (
            patch("model_catalog.is_subscription_enabled", return_value=True),
            patch(
                "completion_router.subscription_model",
                return_value=MagicMock(provider="grok", model="grok-4.5"),
            ),
            patch("subscription_complete.complete_subscription_chain", new=_chain),
            patch("subscription_complete.log_subscription_attempts", new=AsyncMock()),
        ):
            from subscription_invoke import complete_text

            out = await complete_text(
                "grok-4.5",
                config={"subscription_models": []},
                prompt="hi",
                surface="research",
                timeout_s=45,
            )
        self.assertEqual(out, "ok")
        self.assertIsNotNone(captured)
        self.assertEqual(captured[0].timeout_s, 45)

    async def test_complete_typed_passes_output_schema(self):
        captured: list[object] = []

        async def _chain(req, cfg, **kw):
            captured.append(req)
            from completion_router import CompletionResult
            return CompletionResult(provider="grok", model="grok-4.5", text='{"score": 9}'), []

        with (
            patch("model_catalog.is_subscription_enabled", return_value=True),
            patch(
                "completion_router.subscription_model",
                return_value=MagicMock(provider="grok", model="grok-4.5"),
            ),
            patch("subscription_complete.complete_subscription_chain", new=_chain),
            patch("subscription_complete.log_subscription_attempts", new=AsyncMock()),
        ):
            from subscription_invoke import complete_typed

            parsed = await complete_typed(
                "grok-4.5",
                _JudgeOut,
                config={"subscription_models": []},
                prompt="judge",
                surface="judge",
            )
        self.assertIsInstance(parsed, _JudgeOut)
        self.assertEqual(parsed.score, 9)
        self.assertIsInstance(captured[0].output_schema, dict)
        self.assertIn("properties", captured[0].output_schema)
        self.assertNotIn("$defs", captured[0].output_schema)


if __name__ == "__main__":
    unittest.main()
