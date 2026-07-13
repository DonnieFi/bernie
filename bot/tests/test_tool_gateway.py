"""ToolGateway tests — RBAC, validation, shadow blocking, unknown tools."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from executor import ServiceRefs, ToolContext


def _make_ctx(group="all", shadow=False, config=None, hitl_approved=False):
    refs = ServiceRefs(
        calendar=None, ha=None, db=None, session=None,
        orchestrator=None, identity=None, tz=None,
    )
    return ToolContext(
        config=config or {},
        person_id=None,
        group=group,
        channel_id=None,
        shadow=shadow,
        executor="native",
        services=refs,
        hitl_approved=hitl_approved,
    )


def _gateway():
    from tools import get_registry, load_all_domains
    from tool_gateway import ToolGateway

    load_all_domains()
    return ToolGateway(registry=get_registry())


class TestTruncateToolResult(unittest.TestCase):
    def test_json_snapshot_stays_valid_json(self):
        import json
        from tool_gateway import _truncate_tool_result

        payload = json.dumps({
            "summary": "ok " * 50,
            "core": {"phase": "en_route", "ident": "OCN74"},
            "raw": {"flight": {"x": "y" * 5000}},
        })
        out = _truncate_tool_result(payload, cap=800)
        self.assertLessEqual(len(out), 800)
        data = json.loads(out)
        self.assertIn("core", data)
        self.assertNotIn("raw", data)

    def test_plain_string_still_truncated(self):
        from tool_gateway import _truncate_tool_result

        out = _truncate_tool_result("x" * 200, cap=50)
        self.assertTrue(out.startswith("x" * 50) or "truncated" in out)
        self.assertLessEqual(len(out), 50 + 80)


class TestToolGateway(unittest.TestCase):
    def test_unknown_tool_returns_error(self):
        gw = _gateway()
        result = asyncio.run(gw.execute("nonexistent_tool", {}, _make_ctx()))
        self.assertIn("unknown tool", result.lower())

    def test_rbac_blocks_admin_tool_for_family(self):
        """A caller in the 'all' group (family) cannot call an admin-only tool."""
        gw = _gateway()
        result = asyncio.run(gw.execute("litellm_switch_model", {"model_name": "x"}, _make_ctx(group="all")))
        self.assertTrue("denied" in result.lower() or "restricted" in result.lower(), result)

    def test_rbac_blocks_admin_tool_for_kids(self):
        gw = _gateway()
        result = asyncio.run(gw.execute("trigger_system_audit", {}, _make_ctx(group="kids")))
        self.assertTrue(
            "restricted" in result.lower() or "denied" in result.lower() or "sorry" in result.lower(),
            result,
        )

    def test_rbac_blocks_parents_tool_for_family(self):
        """A caller in 'all' (or kids) cannot call a parents-only tool."""
        gw = _gateway()
        result = asyncio.run(gw.execute("create_task", {"title": "X", "assigned_to": "Child1"}, _make_ctx(group="all")))
        self.assertTrue(
            "denied" in result.lower() or "restricted" in result.lower() or "sorry" in result.lower(),
            result,
        )

    def test_rbac_admin_can_call_admin_tool(self):
        """Admin group passes RBAC and reaches the handler (handler may still fail
        on missing services — we just verify it gets past RBAC)."""
        gw = _gateway()
        result = asyncio.run(gw.execute(
            "litellm_switch_model", {"model_name": "claude-sonnet-4-6"},
            _make_ctx(group="admin", shadow=True),
        ))
        self.assertIn("shadow", result.lower())

    def test_schema_validation_raises_tool_validation_error(self):
        from tool_gateway import ToolValidationError
        gw = _gateway()
        with self.assertRaises(ToolValidationError) as cm:
            asyncio.run(gw.execute(
                "create_event", {"summary": "Test"},
                _make_ctx(group="all", shadow=True),
            ))
        self.assertEqual(cm.exception.tool_name, "create_event")
        self.assertIn("invalid", cm.exception.message.lower())

    def test_coerces_start_end_to_start_date_end_date(self):
        """DeepSeek and other non-Anthropic models often use start/end aliases."""
        gw = _gateway()
        result = asyncio.run(gw.execute(
            "get_events_range",
            {"start": "2026-06-30", "end": "2026-07-31"},
            _make_ctx(group="all", shadow=True),
        ))
        # Shadow mode still dispatches read tools; without calendar service we get a stub error.
        self.assertNotIn("'start_date' is a required property", result.lower())

    def test_shadow_write_blocked(self):
        gw = _gateway()
        result = asyncio.run(gw.execute(
            "create_event", {"summary": "x", "date": "2026-06-01", "time": "10:00"},
            _make_ctx(group="all", shadow=True),
        ))
        self.assertIn("shadow", result.lower())

    def test_shadow_read_executes(self):
        """Read tools should not be blocked in shadow mode."""
        gw = _gateway()
        result = asyncio.run(gw.execute(
            "list_automations", {},
            _make_ctx(group="all", shadow=True),
        ))
        self.assertNotIn("[shadow:", result)

    def test_get_tool_schemas_filters_by_group(self):
        gw = _gateway()
        all_schemas = gw.get_tool_schemas("all")
        admin_schemas = gw.get_tool_schemas("admin")
        self.assertGreater(
            len(admin_schemas),
            len(all_schemas),
            f"admin should have access to more tools than 'all' (admin={len(admin_schemas)}, all={len(all_schemas)})",
        )

    def test_get_tool_schemas_no_metadata_leaks(self):
        """The schemas returned to Anthropic must not contain role_required,
        is_write, or 'fn' — Anthropic API rejects unknown fields."""
        gw = _gateway()
        schemas = gw.get_tool_schemas("admin")
        for s in schemas:
            self.assertLessEqual(set(s.keys()), {"name", "description", "input_schema"}, f"Unexpected keys in schema for {s['name']}: {s.keys()}")

    def test_litellm_switch_model_validation(self):
        gw = _gateway()
        config = {
            "anthropic_models": ["claude-sonnet-4-6"],
            "litellm_models": ["or-deepseek-v4"],
            "ollama_models": ["gemma4:e4b"]
        }
        
        # Test empty name
        result = asyncio.run(gw.execute(
            "litellm_switch_model", {"model_name": "   "},
            _make_ctx(group="admin", shadow=False, config=config, hitl_approved=True),
        ))
        self.assertIn("error", result.lower())
        self.assertIn("cannot be empty", result.lower())

        # Test unregistered name
        result = asyncio.run(gw.execute(
            "litellm_switch_model", {"model_name": "invalid-model"},
            _make_ctx(group="admin", shadow=False, config=config, hitl_approved=True),
        ))
        self.assertIn("error", result.lower())
        self.assertIn("not registered", result.lower())
