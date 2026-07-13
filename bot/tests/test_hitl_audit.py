import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.modules["audioop"] = MagicMock()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database as test_db
from executor import ServiceRefs, ToolContext
from hitl.hitl_discord import post_tier2_anvil_audit, set_anvil_audit_bot
from tools import get_registry
from tool_gateway import ToolGateway


class TestHitlAudit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old_db_path = test_db.DB_PATH
        test_db.DB_PATH = self._tmp.name
        await test_db.init_db()

        self.services = ServiceRefs(db=test_db)
        self.gateway = ToolGateway(registry=get_registry())
        self.executed_count = 0

        from tools import tool

        @tool(
            name="__test_audit_tier1",
            description="tier 1",
            input_schema={},
            role_required="all",
            is_write=False,
            tier=1,
        )
        async def handle_tier1(args, ctx):
            self.executed_count += 1
            return "ok"

        @tool(
            name="__test_audit_tier2",
            description="tier 2",
            input_schema={},
            role_required="all",
            is_write=True,
            tier=2,
        )
        async def handle_tier2(args, ctx):
            self.executed_count += 1
            return "ok"

        @tool(
            name="__test_audit_tier3",
            description="tier 3",
            input_schema={},
            role_required="all",
            is_write=True,
            tier=3,
        )
        async def handle_tier3(args, ctx):
            self.executed_count += 1
            return "ok"

        self._cleanup_tools = ("__test_audit_tier1", "__test_audit_tier2", "__test_audit_tier3")

    async def asyncTearDown(self):
        registry = get_registry()
        for name in self._cleanup_tools:
            registry.pop(name, None)
        set_anvil_audit_bot(None)
        test_db.DB_PATH = self._old_db_path
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def _make_ctx(self) -> ToolContext:
        return ToolContext(
            config={"anvil_channel_id": 99999},
            person_id="person.red",
            group="admin",
            channel_id="222222222222222222",
            shadow=False,
            executor="native",
            services=self.services,
        )

    async def test_tier2_posts_anvil_audit(self):
        mock_channel = AsyncMock()
        mock_bot = MagicMock()
        mock_bot.get_channel.return_value = mock_channel
        set_anvil_audit_bot(mock_bot)

        with patch.dict(os.environ, {"ROLE": "discord"}, clear=False):
            ctx = self._make_ctx()
            res = await self.gateway.execute("__test_audit_tier2", {"title": "x"}, ctx)
            self.assertEqual(res, "ok")
            for _ in range(30):
                if mock_channel.send.await_count:
                    break
                await asyncio.sleep(0.05)
        mock_channel.send.assert_awaited_once()
        body = mock_channel.send.await_args.args[0]
        self.assertIn("__test_audit_tier2", body)
        self.assertIn("person.red", body)

    async def test_tier1_does_not_post_anvil_audit(self):
        mock_channel = AsyncMock()
        mock_bot = MagicMock()
        mock_bot.get_channel.return_value = mock_channel
        set_anvil_audit_bot(mock_bot)

        with patch.dict(os.environ, {"ROLE": "discord"}, clear=False):
            ctx = self._make_ctx()
            await self.gateway.execute("__test_audit_tier1", {}, ctx)
            await asyncio.sleep(0.15)
        mock_channel.send.assert_not_awaited()

    async def test_tier3_hold_does_not_post_anvil_audit(self):
        mock_channel = AsyncMock()
        mock_bot = MagicMock()
        mock_bot.get_channel.return_value = mock_channel
        set_anvil_audit_bot(mock_bot)

        with patch.dict(os.environ, {"ROLE": "discord"}, clear=False):
            with patch("hitl.hitl_discord.get_inline_notifier", return_value=None):
                ctx = self._make_ctx()
                res = await self.gateway.execute("__test_audit_tier3", {}, ctx)
            self.assertIn("requires admin approval", res)
            await asyncio.sleep(0.15)
        mock_channel.send.assert_not_awaited()

    async def test_skips_when_role_is_cognition(self):
        mock_channel = AsyncMock()
        mock_bot = MagicMock()
        mock_bot.get_channel.return_value = mock_channel
        set_anvil_audit_bot(mock_bot)

        with patch.dict(os.environ, {"ROLE": "cognition"}, clear=False):
            await post_tier2_anvil_audit(
                tool_name="foo",
                args={},
                ctx=self._make_ctx(),
            )

        mock_channel.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
