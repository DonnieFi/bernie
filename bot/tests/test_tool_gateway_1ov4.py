"""family-bot-1ov.4: ToolGateway does not load domains on every execute."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tool_gateway import ToolGateway


class TestGatewayLoadOnce(unittest.IsolatedAsyncioTestCase):
    async def test_execute_skips_load_when_registry_warm(self):
        async def _handler(args, ctx):
            return "ok"

        registry = {
            "ping": {
                "name": "ping",
                "fn": _handler,
                "description": "ping",
                "input_schema": {"type": "object", "properties": {}},
                "role_required": "all",
                "is_write": False,
                "tier": 1,
                "domain": "discovery",
            }
        }
        gw = ToolGateway(registry=registry)
        ctx = MagicMock()
        ctx.group = "admin"
        ctx.person_id = "dad"
        ctx.shadow = False
        ctx.channel_id = "c"
        ctx.config = {}
        ctx.prompt_hash = None
        ctx.services = MagicMock(db=None)

        with patch("tools.load_all_domains") as mock_load:
            # tools.load_all_domains is imported inside execute only on cold start
            with patch.object(gw, "_registry", registry):
                res = await gw.execute("ping", {}, ctx)
        self.assertEqual(res, "ok")
        mock_load.assert_not_called()

    async def test_execute_cold_start_loads_when_empty(self):
        gw = ToolGateway(registry={})
        ctx = MagicMock()
        ctx.group = "admin"
        ctx.person_id = "dad"
        ctx.shadow = False
        ctx.channel_id = "c"
        ctx.config = {}
        ctx.prompt_hash = None
        ctx.services = MagicMock(db=None)

        def _fill():
            async def _handler(args, ctx):
                return "warmed"

            gw._registry["ping"] = {
                "name": "ping",
                "fn": _handler,
                "description": "ping",
                "input_schema": {"type": "object", "properties": {}},
                "role_required": "all",
                "is_write": False,
                "tier": 1,
                "domain": "discovery",
            }

        with patch("tools.load_all_domains", side_effect=_fill) as mock_load:
            res = await gw.execute("ping", {}, ctx)
        self.assertEqual(res, "warmed")
        mock_load.assert_called_once()


if __name__ == "__main__":
    unittest.main()
