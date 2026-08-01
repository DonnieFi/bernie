""" /api/health provider_readiness contract (subscription follow-up)."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestApiHealthSubscription(unittest.IsolatedAsyncioTestCase):
    async def test_health_includes_provider_readiness(self):
        from api.routes.health import build_health_router
        import api.common as ac

        ac.config = {
            "ollama_models": ["qwen"],
            "ollama_base_url": "http://192.168.1.X:11434",
            "subscription_models": [],
        }
        ac.BOT_START_TIME = __import__("datetime").datetime.now()
        ac.get_model_info = lambda: ("grok-4.5", None)
        ac._format_uptime = lambda s: "1m"

        bot = MagicMock()
        bot.is_ready.return_value = True
        router = build_health_router(MagicMock(bot=bot))
        route = next(r for r in router.routes if getattr(r, "path", "") == "/api/health")
        handler = route.endpoint

        with (
            patch("model_registry.model_source", return_value="grok"),
            patch(
                "subscription_complete.fetch_runner_health",
                new=AsyncMock(return_value={"providers": {"grok": "ready", "codex": "unavailable"}}),
            ),
        ):
            payload = await handler()

        self.assertEqual(payload["provider"], "grok")
        readiness = payload["provider_readiness"]
        self.assertIn("grok", readiness)
        self.assertIn("codex", readiness)
        self.assertEqual(readiness["grok"], "ready")
        self.assertNotIn("token", str(payload))


if __name__ == "__main__":
    unittest.main()
