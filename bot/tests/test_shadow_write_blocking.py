# tests/test_shadow_write_blocking.py
import sys, os, asyncio, unittest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from executor import ToolContext, ServiceRefs, ExecutorConfig
from unittest.mock import patch


class TestShadowWriteBlocking(unittest.TestCase):

    def test_write_tool_blocked_in_shadow(self):
        from tools import load_all_domains, get_registry
        from tool_gateway import ToolGateway
        load_all_domains()
        gw = ToolGateway(registry=get_registry())
        refs = ServiceRefs(calendar=None, ha=None, db=None, session=None, orchestrator=None, identity=None, tz=None)
        ctx = ToolContext(config={}, person_id=None, group="admin", channel_id=None,
                          shadow=True, executor="smol", services=refs)

        with patch('tool_gateway.jsonschema.validate'):
            for write_tool in ["create_event", "send_email", "control_device", "notify_family_member"]:
                if write_tool in get_registry():
                    result = asyncio.run(gw.execute(write_tool, {}, ctx))
                    self.assertIn("shadow", result.lower(),
                                  f"{write_tool} should be shadow-blocked, got: {result}")

    def test_read_tool_executes_in_shadow(self):
        from tools import load_all_domains, get_registry
        from tool_gateway import ToolGateway
        from unittest.mock import MagicMock, AsyncMock
        load_all_domains()
        gw = ToolGateway(registry=get_registry())
        cal = MagicMock()
        cal.get_todays_events = AsyncMock(return_value=[])
        cal.events_to_text = MagicMock(return_value="No events")
        from zoneinfo import ZoneInfo
        refs = ServiceRefs(calendar=cal, ha=None, db=None, session=None, orchestrator=None, identity=None,
                           tz=ZoneInfo("America/Halifax"))
        ctx = ToolContext(config={}, person_id=None, group="family", channel_id=None,
                          shadow=True, executor="smol", services=refs)
        result = asyncio.run(gw.execute("get_todays_events", {}, ctx))
        self.assertEqual(result, "No events")

    def test_shadow_tool_metadata_includes_prompt_hash(self):
        from tool_gateway import ToolGateway

        gw = ToolGateway(registry={})
        captured = {}

        async def fake_routed(op, *args, **kwargs):
            if op == "log_activity":
                captured.update(kwargs)
            return None

        from unittest.mock import MagicMock

        # _emit_activity early-returns when services.db is missing
        ctx = ToolContext(
            config={},
            person_id="dad",
            group="family",
            channel_id=None,
            shadow=True,
            executor="smol",
            services=ServiceRefs(db=MagicMock()),
            prompt_hash="abc123",
        )

        # Production path uses db_writes.routed("log_activity", ...), not db.log_activity
        with patch("tool_gateway.db_writes.routed", side_effect=fake_routed):
            asyncio.run(gw._emit_activity("get_todays_events", ctx))

        self.assertIn("meta", captured)
        self.assertTrue(
            str(captured["meta"]).endswith("prompt_hash=abc123"),
            captured.get("meta"),
        )


if __name__ == "__main__":
    unittest.main()
