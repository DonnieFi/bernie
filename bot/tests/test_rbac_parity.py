import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestRbacParity(unittest.TestCase):
    def test_family_tools_match(self):
        from tools import get_registry, load_all_domains
        from tool_gateway import ToolGateway

        load_all_domains()
        gw = ToolGateway(registry=get_registry())
        new_names = {t["name"] for t in gw.get_tool_schemas("family")}
        for expected in ["get_todays_events", "get_current_weather", "who_is_home", "web_search"]:
            self.assertIn(expected, new_names, f"{expected} missing from family tools")

    def test_admin_has_more_tools_than_family(self):
        from tools import get_registry, load_all_domains
        from tool_gateway import ToolGateway

        load_all_domains()
        gw = ToolGateway(registry=get_registry())
        family = gw.get_tool_schemas("family")
        admin = gw.get_tool_schemas("admin")
        self.assertGreater(len(admin), len(family), "admin should have more tools than family")

    def test_admin_only_tools_blocked_for_kids(self):
        from executor import ServiceRefs, ToolContext
        from tools import get_registry, load_all_domains
        from tool_gateway import ToolGateway

        load_all_domains()
        gw = ToolGateway(registry=get_registry())
        ctx = ToolContext(
            config={}, person_id=None, group="kids",
            channel_id=None, shadow=False, executor="native", services=ServiceRefs(),
        )
        result = asyncio.run(gw.execute("litellm_switch_model", {"model": "x"}, ctx))
        self.assertTrue(
            "denied" in result.lower() or "restricted" in result.lower(),
            result,
        )

    def test_ported_stragglers_in_registry(self):
        """Phase 27 T8.3b–e: 4 tools moved from _execute_tool into tools/."""
        from tools import get_registry, load_all_domains

        load_all_domains()
        names = set(get_registry().keys())
        for expected in ["get_garbage_schedule", "get_oura_sleep", "get_camera_snapshot", "send_email"]:
            self.assertIn(expected, names, f"{expected} missing from registry")

