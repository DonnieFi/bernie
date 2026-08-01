"""Regression: ToolGateway must resolve tools in cognition-style cold starts."""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tool_gateway import ToolGateway


class TestToolGatewayRegistryColdStart(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tool_gateway import reset_tool_gateway_for_tests
        reset_tool_gateway_for_tests()

    async def test_execute_loads_domains_when_registry_was_empty_at_init(self):
        import tools as tools_mod
        from tools import get_registry

        saved_registry = dict(tools_mod._registry)
        saved_loaded = tools_mod._domains_loaded
        try:
            tools_mod._registry.clear()
            tools_mod._domains_loaded = True  # domains imported earlier; registry cleared
            gw = ToolGateway(registry=get_registry())
            self.assertNotIn("send_email", gw._registry)

            ctx = MagicMock()
            ctx.shadow = True
            ctx.group = "system"
            ctx.person_id = "agent:test"
            ctx.config = {}
            ctx.services = MagicMock()

            result = await gw.execute(
                "send_email",
                {"to": "a@b.com", "subject": "s", "body": "b"},
                ctx,
            )
            self.assertIn("send_email", gw._registry)
            self.assertIn("[shadow:", result)
        finally:
            tools_mod._registry.clear()
            tools_mod._registry.update(saved_registry)
            tools_mod._domains_loaded = saved_loaded


if __name__ == "__main__":
    unittest.main()
