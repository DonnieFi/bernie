"""family-bot-5hy.2: get_usage_costs tool rollups."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


class TestGetUsageCosts(unittest.IsolatedAsyncioTestCase):
    async def test_formats_totals_and_models(self):
        from tools.admin import handle_get_usage_costs

        db = MagicMock()
        db.get_token_usage_stats = AsyncMock(
            return_value={
                "totalUsd": 1.2345,
                "days": [
                    {"day": "2026-07-01", "in": 100, "out": 50, "usd": 0.5},
                    {"day": "2026-07-02", "in": 200, "out": 80, "usd": 0.7345},
                ],
            }
        )
        db.get_token_usage_summary = AsyncMock(
            return_value={
                "claude-sonnet-4-6": {
                    "input": 1000,
                    "output": 200,
                    "requests": 5,
                    "cost": 1.0,
                },
                "or-deepseek": {
                    "input": 500,
                    "output": 100,
                    "requests": 2,
                    "cost": 0.2345,
                },
            }
        )
        db.get_top_sessions = AsyncMock(return_value=[])

        ctx = SimpleNamespace(shadow=False, services=SimpleNamespace())
        with patch("db_binding.get_database", return_value=db):
            text = await handle_get_usage_costs({"days": 7}, ctx)

        self.assertIn("$1.2345", text)
        self.assertIn("claude-sonnet-4-6", text)
        self.assertIn("By model", text)
        self.assertIn("By day", text)
        db.get_top_sessions.assert_not_called()

    async def test_include_sessions(self):
        from tools.admin import handle_get_usage_costs

        db = MagicMock()
        db.get_token_usage_stats = AsyncMock(return_value={"totalUsd": 0.1, "days": []})
        db.get_token_usage_summary = AsyncMock(return_value={})
        db.get_top_sessions = AsyncMock(
            return_value=[{"id": "ch-1", "title": "Hello", "cost": 0.05, "modelId": "claude"}]
        )
        ctx = SimpleNamespace(shadow=False)
        with patch("db_binding.get_database", return_value=db):
            text = await handle_get_usage_costs(
                {"days": 3, "include_sessions": True}, ctx
            )
        self.assertIn("Top sessions", text)
        self.assertIn("Hello", text)
        db.get_top_sessions.assert_awaited()


if __name__ == "__main__":
    unittest.main()
