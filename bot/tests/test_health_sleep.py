"""Tests for health/sleep prefetch and routing."""
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from health_sleep import looks_health_sleep_query, prefetch_health_sleep


class TestLooksHealthSleepQuery(unittest.TestCase):
    def test_detects_sleep_last_night(self):
        self.assertTrue(looks_health_sleep_query("how well did i sleep last night", {}))

    def test_detects_wearable_compare(self):
        self.assertTrue(
            looks_health_sleep_query("compare my oura and garmin sleep scores", {})
        )

    def test_detects_garmin_oura_without_sleep_word(self):
        self.assertTrue(looks_health_sleep_query("compare garmin and oura", {}))

    def test_ignores_unrelated_compare(self):
        self.assertFalse(
            looks_health_sleep_query("compare tomorrow weather and calendar", {})
        )

    def test_respects_custom_patterns(self):
        cfg = {"executor": {"health_sleep_patterns": [r"\btracker\b"]}}
        self.assertTrue(looks_health_sleep_query("check my tracker", cfg))
        self.assertFalse(looks_health_sleep_query("how did i sleep", cfg))


class TestPrefetchHealthSleepBlock(unittest.IsolatedAsyncioTestCase):
    async def test_prefetch_calls_both_tools(self):
        garmin_payload = json.dumps(
            {"summary": "Dad: score 59", "core": {"sleep_score": 59}, "extras": None}
        )
        oura_payload = json.dumps({"date": "2026-06-02", "daily_score": 78})

        services = MagicMock()
        mock_gw = MagicMock()
        mock_gw.execute = AsyncMock(side_effect=[garmin_payload, oura_payload])
        with patch("tool_gateway.get_tool_gateway", return_value=mock_gw):
            status = await prefetch_health_sleep(
                config={},
                services=services,
                person_id="dad",
                group="admin",
                channel_id="111111111111111111",
            )

        self.assertEqual(mock_gw.execute.await_count, 2)
        self.assertTrue(status.ok)
        self.assertIn("AUTHORITATIVE HEALTH DATA", status.block or "")
        self.assertIn(garmin_payload, status.block or "")
        self.assertIn(oura_payload, status.block or "")

    async def test_prefetch_flags_skipped_source(self):
        services = MagicMock()
        mock_gw = MagicMock()
        mock_gw.execute = AsyncMock(side_effect=[
            "No sleep profile for person='dad' source='garmin'.",
            '{"date": "2026-06-02", "daily_score": 78}',
        ])
        with patch("tool_gateway.get_tool_gateway", return_value=mock_gw):
            status = await prefetch_health_sleep(
                config={},
                services=services,
                person_id="dad",
                group="admin",
                channel_id=None,
            )
        self.assertFalse(status.ok)
        self.assertFalse(status.garmin_ok)
        self.assertTrue(status.oura_ok)


class TestHealthSleepRouting(unittest.TestCase):
    def _route(self, user_message, exec_cfg=None):
        import config as config_mod
        import claude_service
        from executor import ServiceRefs

        exec_cfg = exec_cfg or {
            "chat": "smol",
            "chat_routing": "intent",
            "smol_models": ["or-deepseek-v4"],
        }
        orig = config_mod.config
        try:
            config_mod.config = {"executor": exec_cfg}
            return claude_service._get_executor(
                "chat",
                ServiceRefs(),
                model="or-deepseek-v4",
                user_message=user_message,
            )
        finally:
            config_mod.config = orig

    def test_wearable_compare_routes_native_on_smol_surface(self):
        from executors.native import NativeToolExecutor

        ex = self._route("compare my oura and garmin sleep scores")
        self.assertIsInstance(ex, NativeToolExecutor)

    def test_executor_override_forces_native(self):
        from executors.native import NativeToolExecutor
        from executors.smol import SmolExecutor

        ex = self._route(
            "what is the weather",
            exec_cfg={"chat": "smol", "smol_models": ["or-deepseek-v4"]},
        )
        self.assertIsInstance(ex, SmolExecutor)
        ex2 = self._route(
            "what is the weather",
            exec_cfg={"chat": "smol", "smol_models": ["or-deepseek-v4"]},
        )
        import config as config_mod
        import claude_service
        from executor import ServiceRefs

        orig = config_mod.config
        try:
            config_mod.config = {
                "executor": {"chat": "smol", "smol_models": ["or-deepseek-v4"]},
            }
            forced = claude_service._get_executor(
                "chat",
                ServiceRefs(),
                model="or-deepseek-v4",
                user_message="what is the weather",
                executor_override="native",
            )
            self.assertIsInstance(forced, NativeToolExecutor)
        finally:
            config_mod.config = orig


if __name__ == "__main__":
    unittest.main()
