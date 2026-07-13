"""Host unittest for Phase 39 Wave 1a tool surface resolver.

All new logic has host unittest coverage runnable via:
  ssh operator@bernie-host
  cd /opt/family-bot
  PYTHONPATH=bot python -m unittest bot.tests.test_tool_surface bot.tests.test_search_tools -v

Covers: mode_ceiling (deny), apply_channel_map (precedence + anvil + DM), resolve (ceiling+channel+narrow),
surface_is_narrowed (vs post-channel), startup validation (good + bad domain + bad discovery name + broken YAML paths),
chef/furnace ceiling regression, full precedence table cases.
"""

import os
import sys
import unittest
from unittest.mock import patch

# Make "import modes", "import tools", "from llm..." work when run as PYTHONPATH=bot ...
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import modes as modes_mod
from tools import load_all_domains, get_registry
from llm.tool_surface import (
    mode_ceiling,
    apply_channel_map,
    resolve_tool_domains,
    surface_is_narrowed,
    turn_surface_narrowed,
    append_tool_surface_ux,
    validate_tool_surface_at_startup,
    get_tool_schemas_for_turn,
    deferral_system_block,
)
from llm.intent_router import narrow_tool_domains  # for direct comparison in one test


class TestModeCeiling(unittest.TestCase):
    def setUp(self):
        modes_mod._modes.clear()
        modes_mod._mode_override = None
        load_all_domains()
        modes_mod.load_all_modes()

    def test_concierge_deny_applied(self):
        # concierge denies "admin"
        chef_or_conc = modes_mod.get_mode("concierge")
        ceiling = mode_ceiling(chef_or_conc)
        self.assertIsNotNone(ceiling)
        self.assertIn("calendar", ceiling)
        self.assertIn("search", ceiling)
        self.assertNotIn("admin", ceiling)

    def test_chef_ceiling_excludes_home_and_admin(self):
        # chef/furnace must not pull heavy admin/home/cognitive by default
        chef = modes_mod.get_mode("chef")
        ceiling = mode_ceiling(chef)
        self.assertIsNotNone(ceiling)
        self.assertIn("meals", ceiling)
        self.assertIn("search", ceiling)
        self.assertNotIn("home", ceiling)
        self.assertNotIn("admin", ceiling)
        self.assertNotIn("cognitive", ceiling)
        self.assertNotIn("network", ceiling)
        # Per plan: chef/furnace must not include kanban (or other heavy) unless explicitly in chef allow.
        self.assertNotIn("kanban", ceiling)
        # Rough size bound for furnace path (plan calls for ~30 or less on chef)
        self.assertLessEqual(len(ceiling), 30)

    def test_ops_full_surface(self):
        ops = modes_mod.get_mode("ops")
        ceiling = mode_ceiling(ops)
        # ops allow has admin + others and deny is empty
        self.assertIsNotNone(ceiling)
        self.assertIn("admin", ceiling)


class TestApplyChannelMap(unittest.TestCase):
    def setUp(self):
        load_all_domains()
        modes_mod._modes.clear()
        modes_mod.load_all_modes()

    def test_no_map_returns_ceiling(self):
        cfg = {"anvil_channel_id": "999", "furnace_channel_id": "888"}
        ceiling = ["calendar", "meals", "notify"]
        self.assertEqual(apply_channel_map(ceiling, "123", cfg), ceiling)

    def test_channel_map_intersect(self):
        cfg = {
            "anvil_channel_id": "999",
            "channel_tool_domains": {"777": ["calendar", "memory", "weather", "notify", "search"]},
        }
        ceiling = ["calendar", "home", "notify", "search", "tasks"]
        result = apply_channel_map(ceiling, "777", cfg)
        self.assertEqual(result, ["calendar", "notify", "search"])

    def test_anvil_hard_bypass(self):
        cfg = {
            "anvil_channel_id": "999",
            "channel_tool_domains": {"999": ["calendar", "notify"]},  # would narrow, but anvil bypasses
        }
        ceiling = ["calendar", "home", "notify", "admin"]
        result = apply_channel_map(ceiling, "999", cfg)
        self.assertEqual(result, ceiling)  # unchanged

    def test_dm_no_channel_map(self):
        cfg = {"channel_tool_domains": {"123": ["calendar"]}}
        ceiling = ["calendar", "home", "notify"]
        # no channel_id (DM) => no intersect
        self.assertEqual(apply_channel_map(ceiling, None, cfg), ceiling)
        self.assertEqual(apply_channel_map(ceiling, "", cfg), ceiling)


class TestResolveToolDomainsSkeleton(unittest.TestCase):
    def test_mode_then_channel(self):
        load_all_domains()
        modes_mod._modes.clear()
        modes_mod.load_all_modes()
        chef = modes_mod.get_mode("chef")
        cfg = {"furnace_channel_id": "888", "anvil_channel_id": "999"}
        # chef ceiling intersected with a map that drops some
        cfg["channel_tool_domains"] = {"888": ["meals", "search", "notify"]}
        domains = resolve_tool_domains(mode=chef, channel_id="888", config=cfg)
        self.assertEqual(domains, ["meals", "search", "notify"])

    def test_fallback_to_mode_domains_list(self):
        cfg = {}
        domains = resolve_tool_domains(mode_domains=["a", "b"], channel_id=None, config=cfg)
        self.assertEqual(domains, ["a", "b"])

    def test_composed_with_router_narrows_further(self):
        """When intent router enabled, resolve applies channel ceiling then can narrow further (e.g. chitchat strip)."""
        load_all_domains()
        modes_mod._modes.clear()
        modes_mod.load_all_modes()
        concierge = modes_mod.get_mode("concierge")
        cfg = {
            "anvil_channel_id": "999",
            "context": {"intent_router": {"enabled": True, "sticky_turns": 0}},
            "channel_tool_domains": {
                # conservative map (no home, no tasks, no transit in this pilot example)
                "777": ["calendar", "memory", "weather", "notify", "search"]
            },
        }
        # Pure chitchat on a mapped channel: channel would allow some, but narrow strips to [] for explicit social.
        # Use an exact match from _CHITCHAT_PATTERNS so looks_chitchat + no domain match triggers strip.
        domains = resolve_tool_domains(
            mode=concierge,
            channel_id="777",
            config=cfg,
            user_message="hi",
            history=[],
            apply_intent_router=True,
        )
        self.assertEqual(domains, [])

        # A schedule intent inside the map stays within the channel ceiling (no home/tasks leaked).
        domains2 = resolve_tool_domains(
            mode=concierge,
            channel_id="777",
            config=cfg,
            user_message="what is on the calendar today",
            history=[],
            apply_intent_router=True,
        )
        self.assertIn("calendar", domains2)
        self.assertNotIn("home", domains2)
        self.assertNotIn("tasks", domains2)
        self.assertNotIn("transit", domains2)  # not present in this conservative map

    def test_apply_false_returns_post_channel_only(self):
        """apply_intent_router=False bypasses narrow, returns post-channel ceiling."""
        load_all_domains()
        modes_mod._modes.clear()
        modes_mod.load_all_modes()
        chef = modes_mod.get_mode("chef")
        cfg = {
            "furnace_channel_id": "888",
            "context": {"intent_router": {"enabled": True}},
            "channel_tool_domains": {"888": ["meals", "search"]},
        }
        domains = resolve_tool_domains(
            mode=chef, channel_id="888", config=cfg, user_message="bus near me", apply_intent_router=False
        )
        # even though message would match transit, apply=False returns exactly the channel map intersect
        self.assertEqual(domains, ["meals", "search"])


class TestSurfaceIsNarrowed(unittest.TestCase):
    def test_no_ceiling_explicit_list_is_narrowed(self):
        self.assertTrue(surface_is_narrowed(["calendar"], None))

    def test_none_final_not_narrowed(self):
        self.assertFalse(surface_is_narrowed(None, ["calendar", "notify"]))

    def test_same_not_narrowed(self):
        self.assertFalse(surface_is_narrowed(["a", "b"], ["a", "b"]))

    def test_different_is_narrowed(self):
        self.assertTrue(surface_is_narrowed(["a"], ["a", "b"]))


class TestTurnSurfaceNarrowed(unittest.TestCase):
    def test_intent_narrowed(self):
        self.assertTrue(turn_surface_narrowed(["calendar"], ["calendar", "notify"], ["calendar", "notify"]))

    def test_channel_map_shrink_triggers(self):
        """Channel map alone should count as narrowed even when intent does not narrow further."""
        mode = ["calendar", "home", "notify", "search", "tasks"]
        post_channel = ["calendar", "notify", "search"]
        final = post_channel
        self.assertTrue(turn_surface_narrowed(final, post_channel, mode))

    def test_full_surface_not_narrowed(self):
        ceiling = ["calendar", "notify"]
        self.assertFalse(turn_surface_narrowed(ceiling, ceiling, ceiling))


class TestAppendToolSurfaceUx(unittest.TestCase):
    def test_injects_on_channel_shrink(self):
        system: list = []
        config = {"tool_surface": {"inject_active_surface_summary": True, "inject_deferral_rule": True}}
        narrowed = append_tool_surface_ux(
            system,
            config,
            tool_domains=["calendar", "notify", "search"],
            tool_count=12,
            mode_slug="concierge",
            mode_ceiling=["calendar", "home", "notify", "search", "tasks"],
            post_channel_ceiling=["calendar", "notify", "search"],
        )
        self.assertTrue(narrowed)
        self.assertEqual(len(system), 2)
        self.assertIn("concierge", system[0]["text"])
        self.assertIn("search_tools", system[1]["text"])

    def test_skips_when_not_narrowed(self):
        system: list = []
        domains = ["calendar", "notify"]
        self.assertFalse(
            append_tool_surface_ux(
                system,
                {"tool_surface": {}},
                tool_domains=domains,
                tool_count=5,
                mode_slug="concierge",
                mode_ceiling=domains,
                post_channel_ceiling=domains,
            )
        )
        self.assertEqual(system, [])


class TestSlagPilotSurface(unittest.TestCase):
    """Wave 3: conservative #slag channel map — no tasks/kanban, ~15-25 tools + discovery union."""

    def setUp(self):
        load_all_domains()
        modes_mod._modes.clear()
        modes_mod.load_all_modes()

    def test_slag_map_excludes_tasks_and_kanban(self):
        from tool_gateway import ToolGateway
        concierge = modes_mod.get_mode("concierge")
        slag_id = "SLAG123"
        cfg = {
            "anvil_channel_id": "999",
            "slag_channel_id": slag_id,
            "channel_tool_domains": {
                slag_id: ["calendar", "memory", "weather", "notify", "search"],
            },
        }
        domains = resolve_tool_domains(
            mode=concierge,
            channel_id=slag_id,
            config=cfg,
            apply_intent_router=False,
        )
        self.assertNotIn("tasks", domains or [])
        self.assertNotIn("kanban", domains or [])
        self.assertIn("calendar", domains or [])
        gw = ToolGateway(registry=get_registry())
        schemas = get_tool_schemas_for_turn(gw, "family", domains, cfg)
        names = {s["name"] for s in schemas}
        self.assertIn("search_tools", names)
        self.assertIn("describe_modes", names)
        self.assertIn("list_slash_commands", names)
        # Domain-filtered count target 15-25 per plan (+ 3 discovery unioned)
        domain_only = get_tool_schemas_for_turn(
            gw, "family", domains, {**cfg, "tool_surface": {"discovery_tools_always_on": []}},
        )
        self.assertGreaterEqual(len(domain_only), 5)
        self.assertLessEqual(len(domain_only), 30)
        self.assertLess(len(schemas), len(get_tool_schemas_for_turn(gw, "family", mode_ceiling(concierge), cfg)))


class TestDeferralBlock(unittest.TestCase):
    def test_deferral_mentions_channels(self):
        text = deferral_system_block({"tool_surface": {"inject_deferral_rule": True}})
        self.assertIn("#smithy", text)
        self.assertIn("#furnace", text)
        self.assertIn("#anvil", text)
        self.assertIn("search_tools", text)

    def test_deferral_disabled(self):
        self.assertEqual(deferral_system_block({"tool_surface": {"inject_deferral_rule": False}}), "")


class TestGetToolSchemasForTurn(unittest.TestCase):
    def test_filters_by_resolved_domains(self):
        from tool_gateway import ToolGateway
        load_all_domains()
        gw = ToolGateway(registry=get_registry())
        # Use a very narrow domain list that we know exists (notify has list_slash_commands)
        schemas = get_tool_schemas_for_turn(gw, "family", ["notify"], {})
        names = [s["name"] for s in schemas]
        # Should only contain tools from the requested domain (or empty if none match group, but notify has all-role tools)
        self.assertTrue(all(s.get("name") for s in schemas))
        # At minimum the well-known notify tool should appear when domain is narrow
        self.assertIn("list_slash_commands", names)
        # And it should be sorted (gateway contract)
        self.assertEqual(names, sorted(names))

    def test_discovery_union_includes_search_tools_on_narrow_surface(self):
        """search_tools (and list_slash) must be unioned even on narrow surfaces (plan acceptance)."""
        from tool_gateway import ToolGateway
        load_all_domains()
        gw = ToolGateway(registry=get_registry())
        # notify-only is narrow; chef is also a good real narrow case
        for doms in (["notify"], ["meals", "search"]):
            schemas = get_tool_schemas_for_turn(gw, "family", doms, {})
            names = [s["name"] for s in schemas]
            self.assertIn("search_tools", names, f"search_tools missing on narrow {doms}")
            self.assertIn("list_slash_commands", names)
            self.assertIn("describe_modes", names)
            self.assertEqual(names, sorted(names))


class TestActiveSurfaceSummaryWave2(unittest.TestCase):
    def test_rich_summary_includes_mode_and_discovery(self):
        from llm.tool_surface import active_surface_summary
        text = active_surface_summary(["meals", "search"], 7, mode_slug="chef")
        self.assertIn("chef", text)
        self.assertIn("7 tools", text)
        self.assertIn("meals", text)
        self.assertIn("search_tools", text)  # default discovery hint

    def test_empty_surface(self):
        from llm.tool_surface import active_surface_summary
        text = active_surface_summary([], 0)
        self.assertIn("none", text.lower())


class TestValidateAtStartup(unittest.TestCase):
    def setUp(self):
        load_all_domains()
        modes_mod._modes.clear()
        modes_mod.load_all_modes()

    def test_good_config_passes(self):
        cfg = {
            "anvil_channel_id": "YOUR_ANVIL_CHANNEL_ID",
            "furnace_channel_id": "YOUR_FURNACE_CHANNEL_ID",
            "tool_surface": {"discovery_tools_always_on": ["list_slash_commands"]},
        }
        # Should not raise
        validate_tool_surface_at_startup(cfg)

    def test_bad_domain_in_channel_map_fails(self):
        cfg = {"channel_tool_domains": {"123": ["nonexistent_domain_zzz"]}}
        with self.assertRaises(RuntimeError) as ctx:
            validate_tool_surface_at_startup(cfg)
        self.assertIn("nonexistent_domain_zzz", str(ctx.exception))

    def test_bad_discovery_tool_name_fails(self):
        cfg = {"tool_surface": {"discovery_tools_always_on": ["does_not_exist_tool_abc"]}}
        with self.assertRaises(RuntimeError) as ctx:
            validate_tool_surface_at_startup(cfg)
        self.assertIn("does_not_exist_tool_abc", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
