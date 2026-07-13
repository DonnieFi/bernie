"""@tool decorator + registry tests."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestToolRegistry(unittest.TestCase):
    def test_tool_decorator_registers(self):
        import tools

        @tools.tool(
            name="__test_ping__",
            description="ping",
            input_schema={"type": "object", "properties": {}, "required": []},
            role_required="all",
        )
        async def handle_test_ping(args, ctx):
            return "pong"

        reg = tools.get_registry()
        self.assertIn("__test_ping__", reg)
        entry = reg["__test_ping__"]
        self.assertEqual(entry["role_required"], "all")
        self.assertEqual(entry["description"], "ping")

    def test_tier_kwarg_accepted_and_ignored(self):
        import tools

        @tools.tool(
            name="__test_tier__",
            description="d",
            input_schema={"type": "object", "properties": {}, "required": []},
            tier="2",
        )
        async def handle(args, ctx):
            return "ok"

        self.assertIn("__test_tier__", tools.get_registry())

    def test_is_write_flag_stored(self):
        import tools

        @tools.tool(
            name="__test_write__",
            description="d",
            input_schema={"type": "object", "properties": {}, "required": []},
            is_write=True,
        )
        async def handle(args, ctx):
            return "ok"

        self.assertTrue(tools.get_registry()["__test_write__"]["is_write"])

    def test_default_role_is_all(self):
        import tools

        @tools.tool(
            name="__test_default_role__",
            description="d",
            input_schema={"type": "object", "properties": {}, "required": []},
        )
        async def handle(args, ctx):
            return "ok"

        self.assertEqual(
            tools.get_registry()["__test_default_role__"]["role_required"],
            "all",
        )
