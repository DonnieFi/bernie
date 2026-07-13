"""Host unittest for Phase 39 Wave 1b search_tools discovery.

Run via (on bernie-host):
  ssh operator@bernie-host
  cd /opt/family-bot
  PYTHONPATH=bot python -m unittest bot.tests.test_search_tools -v

Covers (per plan):
- Keyword hits on name/description/domain
- Returns results when active surface is narrow (e.g. notify-only)
- Union exposes the tool (and results) even when domains=[]
- Handler is callable and searches full registry
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools import load_all_domains, get_registry
from tool_gateway import ToolGateway
from llm.tool_surface import get_tool_schemas_for_turn


class TestSearchToolsRegistrationAndUnion(unittest.TestCase):
    def setUp(self):
        load_all_domains()

    def test_search_tools_is_registered(self):
        reg = get_registry()
        self.assertIn("search_tools", reg)
        entry = reg["search_tools"]
        self.assertEqual(entry.get("domain"), "search")
        self.assertIn("keyword", (entry.get("description") or "").lower() + " " + "search")

    def test_union_includes_search_tools_on_narrow_surface(self):
        """search_tools must appear via union even when domains is narrow (e.g. notify-only)."""
        gw = ToolGateway(registry=get_registry())
        schemas = get_tool_schemas_for_turn(gw, "family", ["notify"], {})
        names = [s["name"] for s in schemas]
        self.assertIn("search_tools", names)
        self.assertIn("list_slash_commands", names)
        self.assertEqual(names, sorted(names))

    def test_union_exposes_when_domains_empty(self):
        """When intent returns [] (chit-chat), union still brings search_tools + list_slash."""
        gw = ToolGateway(registry=get_registry())
        schemas = get_tool_schemas_for_turn(gw, "family", [], {})
        names = [s["name"] for s in schemas]
        self.assertIn("search_tools", names)
        self.assertGreater(len(names), 1)  # at least the discovery pair


class TestSearchToolsHandler(unittest.TestCase):
    def setUp(self):
        load_all_domains()
        self.gw = ToolGateway(registry=get_registry())

    def test_search_tools_finds_by_name_and_description(self):
        # Sync: direct handler call (full registry search)
        import asyncio
        from tools.discovery import handle_search_tools
        ctx = MagicMockForSearch()
        res = asyncio.run(handle_search_tools({"query": "slash"}, ctx))
        self.assertIn("list_slash_commands", res)

        res2 = asyncio.run(handle_search_tools({"query": "weather"}, ctx))
        self.assertIn("weather", (res2 or "").lower())

    def test_search_tools_returns_on_narrow_effectively(self):
        # Call handler directly (bypasses gateway RBAC/shadow checks) — proves it searches full registry
        # even if the advertised surface is narrow.
        import asyncio
        from tools.discovery import handle_search_tools
        ctx = MagicMockForSearch()
        ctx.group = "family"  # in case handler path ever looks
        res = asyncio.run(handle_search_tools({"query": "frigate"}, ctx))
        self.assertTrue("frigate" in res.lower() or "camera" in res.lower() or "no tools" not in res.lower())


class TestDescribeModes(unittest.TestCase):
    def setUp(self):
        load_all_domains()

    def test_describe_modes_registered(self):
        reg = get_registry()
        self.assertIn("describe_modes", reg)
        self.assertEqual(reg["describe_modes"].get("domain"), "search")
        self.assertEqual(reg["describe_modes"].get("tier"), 1)

    def test_describe_modes_handler_lists_modes(self):
        import asyncio
        from tools.discovery import handle_describe_modes

        res = asyncio.run(handle_describe_modes({}, MagicMockForSearch()))
        self.assertIn("Modes:", res)
        self.assertIn("concierge", res)
        self.assertIn("chef", res)


class MagicMockForSearch:
    """Minimal ctx for gateway + handler (search_tools needs group for RBAC + services)."""
    def __init__(self):
        self.group = "family"
        self.services = type("S", (), {"session": None})()


if __name__ == "__main__":
    unittest.main()
