"""Tests for tool tiers implementation (Phase 29 Wave A4)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools import load_all_domains, get_registry, effective_tier, _coerce_tier

_EXPECTED_TOOL_COUNT = 119  # ratchet: session_search + wave2 tools


class TestToolTiers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_all_domains()

    def test_registry_tool_count(self):
        registry = get_registry()
        self.assertEqual(
            len(registry),
            _EXPECTED_TOOL_COUNT,
            f"Expected {_EXPECTED_TOOL_COUNT} tools; update test and 29-TOOL-TIERS.md if intentional",
        )

    def test_all_tools_have_valid_tiers(self):
        registry = get_registry()
        self.assertGreater(len(registry), 0, "Registry should not be empty")
        for name, entry in registry.items():
            if name.startswith("__test_"):
                continue
            with self.subTest(tool=name):
                tier = entry.get("tier")
                self.assertIsNotNone(tier, f"Tool '{name}' must have a registered tier")
                self.assertIsInstance(tier, int, f"Tool '{name}' tier must be an int, got {type(tier)}")
                self.assertIn(tier, (1, 2, 3), f"Tool '{name}' tier must be 1, 2, or 3, got {tier}")

    def test_write_tools_are_not_tier_1(self):
        registry = get_registry()
        tier_1_writes = {"send_email"}  # Phase 34: policy + #smithy kid gate, not HITL tier 3
        for name, entry in registry.items():
            if name.startswith("__test_"):
                continue
            if entry.get("is_write") and name not in tier_1_writes:
                with self.subTest(tool=name):
                    tier = entry.get("tier")
                    self.assertNotEqual(tier, 1, f"Effectful tool '{name}' cannot be Tier 1")

    def test_known_danger_tools_are_tier_3(self):
        registry = get_registry()
        danger_tools = {
            "control_device",
            "litellm_switch_model",
            "litellm_add_model",
            "litellm_remove_model",
            "reload_config",
            "frigate_set_camera",
            "frigate_set_hours",
            "switch_mode",
        }
        for name in danger_tools:
            with self.subTest(tool=name):
                self.assertIn(name, registry, f"Danger tool '{name}' should be in registry")
                tier = registry[name].get("tier")
                self.assertEqual(tier, 3, f"Danger tool '{name}' must be Tier 3")

    def test_send_email_is_tier_1(self):
        registry = get_registry()
        self.assertEqual(registry["send_email"].get("tier"), 1)

    def test_coerce_tier_behavior(self):
        self.assertEqual(_coerce_tier(1), 1)
        self.assertEqual(_coerce_tier("2"), 2)
        self.assertEqual(_coerce_tier(3), 3)
        with self.assertRaises(ValueError):
            _coerce_tier(0)
        with self.assertRaises(ValueError):
            _coerce_tier(4)
        with self.assertRaises(ValueError):
            _coerce_tier("invalid")
        self.assertEqual(_coerce_tier(None), 3)

    def test_effective_tier_behavior(self):
        self.assertEqual(effective_tier({"tier": 1}), 1)
        self.assertEqual(effective_tier({"tier": "2"}), 2)
        self.assertEqual(effective_tier({}), 3)
        self.assertEqual(effective_tier({"tier": "invalid"}), 3)


# --- Slash parity tests (drive real registry + ToolGateway path) ---
import database as test_db
from executor import ToolContext, ServiceRefs
from tool_gateway import ToolGateway


class TestSlashToolParity(unittest.IsolatedAsyncioTestCase):
    """New tests for complete slash <-> tool parity. Drive shipped @tool handlers via real gateway."""

    @classmethod
    def setUpClass(cls):
        load_all_domains()
        cls.registry = get_registry()

    async def asyncSetUp(self):
        self._tmp = __import__("tempfile").NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old = test_db.DB_PATH
        test_db.DB_PATH = self._tmp.name
        await test_db.init_db()
        self.services = ServiceRefs(db=test_db)
        self.gw = ToolGateway(registry=self.registry)
        # Seed person registry with discord_id so stop_bus_tracking auto-resolve path succeeds in test ctx
        from constants import registry as person_registry
        person_registry.load({
            "family_members": {
                "dad": {
                    "canonical_id": "dad",
                    "display": "Dad",
                    "discord_id": "123456789012345678",
                    "role": "admin",
                    "aliases": ["don", "dad"]
                }
            }
        })

    async def asyncTearDown(self):
        test_db.DB_PATH = self._old
        try:
            __import__("os").unlink(self._tmp.name)
        except Exception:
            pass
        # Restore person_registry to avoid global mutation affecting other tests
        from constants import registry as person_registry
        try:
            person_registry.load({})  # or previous snapshot; simple clear for test isolation
        except Exception:
            pass

    def _ctx(self, group="family", person="dad"):
        return ToolContext(
            config={},
            person_id=person,
            group=group,
            channel_id="testchan",
            shadow=False,
            executor="native",
            services=self.services,
            prompt_hash="paritytest",
        )

    def test_list_slash_commands_registered_and_runs(self):
        self.assertIn("list_slash_commands", self.registry)
        entry = self.registry["list_slash_commands"]
        self.assertEqual(entry["role_required"], "all")
        self.assertEqual(entry.get("tier"), 1)

    async def test_list_slash_commands_via_gateway(self):
        ctx = self._ctx()
        res = await self.gw.execute("list_slash_commands", {}, ctx)
        self.assertIsInstance(res, str)
        self.assertIn("/weather", res)
        self.assertIn("/reminders", res)
        self.assertIn("list_slash_commands", res)  # self ref
        self.assertIn("/config_summary", res)
        self.assertIn("/config_reminders", res)
        self.assertNotIn("shadow_mode", res.lower())  # exempt not listed? (doc excludes)

    def test_parity_names_present(self):
        must_have = [
            "list_slash_commands",
            "set_reminders",
            "set_dm_mode",
            "get_settings",
            "set_config_summary",
            "set_config_reminders",
            "frigate_set_mode",
            "set_eval_mode",
            "set_worker_model",
            "get_eval_status",
            "stop_bus_tracking",
            "get_temperatures",
            "list_ha_entities",
        ]
        for n in must_have:
            with self.subTest(tool=n):
                self.assertIn(n, self.registry, f"Missing parity tool {n}")

    async def test_prefs_tools_run(self):
        ctx = self._ctx()
        r1 = await self.gw.execute("set_reminders", {"setting": "on"}, ctx)
        self.assertIn("Reminders set to on", r1)
        r2 = await self.gw.execute("set_dm_mode", {"setting": "off"}, ctx)
        self.assertIn("DM mode set to off", r2)
        r3 = await self.gw.execute("get_settings", {}, ctx)
        self.assertIn("reminders=", r3)
        self.assertIn("dm_mode=", r3)

    async def test_admin_config_eval_tools(self):
        # shadow=True version proves dispatch
        ctx = self._ctx(group="admin", person="dad")
        ctx_s = ToolContext(**{**ctx.__dict__, "shadow": True})
        res = await self.gw.execute("set_config_summary", {"hour": 2, "minute": 30}, ctx_s)
        self.assertIn("set_config_summary", res)
        res2 = await self.gw.execute("frigate_set_mode", {"mode": "test"}, ctx_s)
        self.assertIn("frigate_set_mode", res2)
        res3 = await self.gw.execute("get_eval_status", {}, ctx)
        self.assertIn("Shadow Eval Status", res3)

    async def test_admin_config_real_mutation_path(self):
        # Drive the real (non-shadow) handler body for tier-2 config tools.
        # Patch the discord-pulling HITL post (using AsyncMock so _spawn_bg gets a coroutine) to avoid audioop in this test env.
        import sys
        from unittest.mock import patch, MagicMock, AsyncMock
        ctx = self._ctx(group="admin", person="dad")
        with patch.dict(sys.modules, {"hitl.hitl_discord": MagicMock()}):
            sys.modules["hitl.hitl_discord"].post_tier2_anvil_audit = AsyncMock(return_value=None)
            res = await self.gw.execute("set_config_summary", {"hour": 3, "minute": 15}, ctx)
            self.assertIn("03:15", res)
            self.assertNotIn("[shadow", res)
            res2 = await self.gw.execute("set_config_reminders", {"minutes": 45}, ctx)
            self.assertIn("45", res2)
            self.assertNotIn("[shadow", res2)

    async def test_stop_and_temps(self):
        ctx = self._ctx()
        # Without explicit user_id, resolve from seeded ctx.person_id="dad" -> discord_id should succeed
        r = await self.gw.execute("stop_bus_tracking", {}, ctx)
        self.assertNotIn("ensure person has discord_id", r)
        self.assertIn("123456789012345678", r)  # resolved numeric discord id appears in the result path
        r2 = await self.gw.execute("get_temperatures", {}, ctx)
        self.assertIsInstance(r2, str)
        # list_ha
        r3 = await self.gw.execute("list_ha_entities", {"query": "temp"}, ctx)
        self.assertIsInstance(r3, str)
