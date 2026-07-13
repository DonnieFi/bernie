"""Wave-2 non-DB beads: ha_assist, inspect_device, NL schedule, prompt layers."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


class TestParseNlSchedule(unittest.TestCase):
    def test_sunday_morning(self):
        from utils.discord_helpers import parse_nl_schedule

        kind, payload = parse_nl_schedule("every Sunday at 9am")
        self.assertEqual(kind, "weekly")
        self.assertEqual(payload["day_of_week"], 6)
        self.assertEqual(payload["time"], "09:00")

    def test_daily(self):
        from utils.discord_helpers import parse_nl_schedule

        kind, payload = parse_nl_schedule("daily 07:30")
        self.assertEqual(kind, "daily")
        self.assertEqual(payload["time"], "07:30")

    def test_hourly(self):
        from utils.discord_helpers import parse_nl_schedule

        kind, payload = parse_nl_schedule("every hour")
        self.assertEqual(kind, "hourly")
        self.assertEqual(payload["minute"], 0)


class TestHaAssist(unittest.IsolatedAsyncioTestCase):
    async def test_assist_ok(self):
        from tools.home import handle_ha_assist

        ha = MagicMock()
        ha.conversation_process = AsyncMock(
            return_value={
                "ok": True,
                "result": {
                    "response": {"speech": {"plain": {"speech": "Turned on lights"}}}
                },
            }
        )
        ctx = SimpleNamespace(shadow=False, services=SimpleNamespace(ha=ha))
        out = await handle_ha_assist({"text": "turn on kitchen lights"}, ctx)
        self.assertIn("Turned on lights", out)
        ha.conversation_process.assert_awaited()


class TestInspectDevice(unittest.IsolatedAsyncioTestCase):
    async def test_inspect_entity(self):
        from tools.home import handle_inspect_device

        ha = MagicMock()
        ha.resolve_entity_id = MagicMock(return_value="light.kitchen")
        ha.get_state = AsyncMock(
            return_value={
                "entity_id": "light.kitchen",
                "state": "on",
                "attributes": {"friendly_name": "Kitchen"},
            }
        )
        net = MagicMock()
        net.get_devices = AsyncMock(return_value=[])
        ctx = SimpleNamespace(shadow=False, services=SimpleNamespace(ha=ha, network=net))
        with patch("tools.home._ha", return_value=ha), patch(
            "tools.home._network", return_value=net
        ):
            out = await handle_inspect_device({"query": "kitchen light"}, ctx)
        self.assertIn("light.kitchen", out)
        self.assertIn("Home Assistant", out)


class TestPromptLayers(unittest.TestCase):
    def test_cap_truncates(self):
        from context import BernieContext
        import config as cfg_mod

        ctx = BernieContext(
            static_rules="S" * 100,
            dynamic_context="D" * 100,
            tomorrow_context=None,
            routines=[],
            observations=[],
            mode=None,
        )
        old = cfg_mod.config
        try:
            cfg_mod.config = {
                "prompt_layers": {
                    "static_max_tokens": 5,
                    "dynamic_max_tokens": 5,
                    "log_overhead": False,
                }
            }
            blocks = ctx.render_blocks(caching=False)
            self.assertTrue(any("truncated" in b.get("text", "") for b in blocks))
        finally:
            cfg_mod.config = old


class TestBtsRegistrationImport(unittest.TestCase):
    def test_jobs_module_importable(self):
        from jobs import bts_registration

        self.assertTrue(callable(bts_registration.register_discord_bts_tasks))
        self.assertTrue(callable(bts_registration.register_cognition_bts_tasks))

    def test_register_discord_wires_core_tasks(self):
        """Smoke: registration table posts expected task names (8lx.3)."""
        from jobs.bts_registration import register_discord_bts_tasks

        bts = MagicMock()
        m = MagicMock()
        register_discord_bts_tasks(bts, m)
        names = [c.args[0] for c in bts.register.call_args_list]
        for expected in (
            "reminders",
            "daily_summary",
            "watchman",
            "hitl_expiry",
            "external_ip_check",
            "weekly_curator",
        ):
            self.assertIn(expected, names)
        self.assertGreaterEqual(bts.register.call_count, 10)

    def test_register_cognition_wires_overnight(self):
        from jobs.bts_registration import register_cognition_bts_tasks

        bts = MagicMock()
        m = MagicMock()
        register_cognition_bts_tasks(bts, m)
        names = [c.args[0] for c in bts.register.call_args_list]
        for expected in ("nightly_eval", "sqlite_backup", "db_wal_checkpoint"):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
