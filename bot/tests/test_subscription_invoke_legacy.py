"""Legacy complete_text handles MissingProviderClient (review follow-up)."""
from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from completion_router import MissingProviderClient


class TestCompleteTextLegacy(unittest.IsolatedAsyncioTestCase):
    async def test_missing_provider_falls_through_to_topic_helper(self):
        container = MagicMock()
        container.llm_for.side_effect = MissingProviderClient("anthropic", "claude-sonnet")
        fake_worker = types.ModuleType("worker")
        fake_worker._call_ollama_topic = AsyncMock(return_value=("topic-ok", {}))

        with (
            patch("llm.runtime.get_container", return_value=container),
            patch("model_catalog.is_subscription_enabled", return_value=False),
            patch.dict(sys.modules, {"worker": fake_worker}),
        ):
            from subscription_invoke import complete_text

            out = await complete_text(
                "claude-sonnet",
                config={"ollama_models": []},
                prompt="hi",
            )
        self.assertEqual(out, "topic-ok")
        fake_worker._call_ollama_topic.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
