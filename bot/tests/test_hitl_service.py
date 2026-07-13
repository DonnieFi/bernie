import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database as test_db
from executor import ToolContext, ServiceRefs
from tools import tool, get_registry
from tool_gateway import ToolGateway
from hitl.hitl_service import (
    HitlDecision,
    serialize_ctx,
    deserialize_ctx,
    rebuild_tool_context,
    check_tier,
    resume_pending,
    deny_pending,
    run_hitl_expiry_sweep,
    run_hitl_purge,
)


class TestHitlService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_role = os.environ.get("ROLE")
        os.environ["ROLE"] = "monolith"
        from hitl.hitl_discord import get_inline_notifier, set_inline_notifier

        self._old_inline_notifier = get_inline_notifier()
        set_inline_notifier(AsyncMock())
        # Temp DB setup
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old_db_path = test_db.DB_PATH
        test_db.DB_PATH = self._tmp.name
        await test_db.init_db()

        self.services = ServiceRefs(db=test_db)
        self.gateway = ToolGateway(registry=get_registry())

        # Reset registry to avoid pollution, but keep standard tools if needed
        # Actually, let's keep registry but register our mock test tools.
        self.executed_count = 0

        # Define custom test tools
        @tool(name="__test_hitl_read", description="Test read tool", input_schema={}, role_required="all", is_write=False, tier=1)
        async def handle_test_hitl_read(args, ctx):
            self.executed_count += 1
            return "read_ok"

        @tool(name="__test_hitl_write", description="Test write tool", input_schema={}, role_required="all", is_write=True, tier=3)
        async def handle_test_hitl_write(args, ctx):
            self.executed_count += 1
            return "write_ok"

        @tool(name="__test_hitl_tier2", description="Test tier 2 tool", input_schema={}, role_required="all", is_write=True, tier=2)
        async def handle_test_hitl_tier2(args, ctx):
            self.executed_count += 1
            return "tier2_ok"

    async def asyncTearDown(self):
        from hitl.hitl_discord import set_inline_notifier

        set_inline_notifier(self._old_inline_notifier)
        if self._old_role is None:
            os.environ.pop("ROLE", None)
        else:
            os.environ["ROLE"] = self._old_role
        test_db.DB_PATH = self._old_db_path
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass
        # Remove our test tools from registry to clean up
        registry = get_registry()
        registry.pop("__test_hitl_read", None)
        registry.pop("__test_hitl_write", None)
        registry.pop("__test_hitl_tier2", None)

    def _make_ctx(self, shadow=False, hitl_approved=False):
        return ToolContext(
            config={},
            person_id="person:dad",
            group="parents",
            channel_id="channel:123",
            shadow=shadow,
            executor="native",
            services=self.services,
            prompt_hash="dummy_hash",
            task_id=None,
            mode="active_mode",
            hitl_approved=hitl_approved,
        )

    async def test_tier1_proceeds(self):
        ctx = self._make_ctx()
        res = await self.gateway.execute("__test_hitl_read", {}, ctx)
        self.assertEqual(res, "read_ok")
        self.assertEqual(self.executed_count, 1)

    async def test_tier2_logs_stub_and_proceeds(self):
        ctx = self._make_ctx()
        res = await self.gateway.execute("__test_hitl_tier2", {}, ctx)
        self.assertEqual(res, "tier2_ok")
        self.assertEqual(self.executed_count, 1)

        # Verify activity log entry exists for hitl_tier2_stub
        # We need to wait a moment for spawned background task to run
        await asyncio.sleep(0.05)
        async with test_db._db_conn() as db:
            async with db.execute("SELECT * FROM activity_log WHERE event_type = 'hitl_tier2_stub'") as cur:
                row = await cur.fetchone()
                self.assertIsNotNone(row)
                meta_outer = json.loads(row["metadata"])
                meta_inner = json.loads(meta_outer["meta"])
                self.assertEqual(meta_inner["tool_name"], "__test_hitl_tier2")

    async def test_tier3_holds_without_approved(self):
        ctx = self._make_ctx()
        res = await self.gateway.execute("__test_hitl_write", {}, ctx)
        self.assertIn("requires admin approval", res)
        self.assertIn("request #", res)
        self.assertEqual(self.executed_count, 0)

        # Check DB has pending_hitl row
        pending = await test_db.list_pending_hitl(status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["tool_name"], "__test_hitl_write")
        self.assertEqual(pending[0]["status"], "pending")

    async def test_resume_executes_once(self):
        ctx = self._make_ctx()
        hold_msg = await self.gateway.execute("__test_hitl_write", {}, ctx)
        pending_id = int(hold_msg.split("#")[1].split(")")[0])

        res = await resume_pending(pending_id, self.gateway, services=self.services, decided_by="person:dad")
        self.assertEqual(res, "write_ok")
        self.assertEqual(self.executed_count, 1)

        # Check DB row status updated to approved
        row = await test_db.get_pending_hitl(pending_id)
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["decided_by"], "person:dad")

    async def test_approve_after_expiry_no_dispatch(self):
        ctx = self._make_ctx()
        hold_msg = await self.gateway.execute("__test_hitl_write", {}, ctx)
        pending_id = int(hold_msg.split("#")[1].split(")")[0])

        # Manually update expires_at to past in DB
        past_iso = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        async with test_db._db_conn() as db:
            await db.execute("UPDATE pending_hitl SET expires_at = ? WHERE id = ?", (past_iso, pending_id))
            await db.commit()

        # Try to resume
        res = await resume_pending(
            pending_id, self.gateway, services=self.services, decided_by="test-admin",
        )
        self.assertIn("expired", res.lower())
        self.assertEqual(self.executed_count, 0)

        # Verify DB row is expired/unchanged by resume
        row = await test_db.get_pending_hitl(pending_id)
        self.assertEqual(row["status"], "expired")

    async def test_serialize_ctx_allowlist(self):
        ctx = self._make_ctx()
        ctx.extra_custom_attr = "not allowed"

        serialized = serialize_ctx(ctx)
        data = json.loads(serialized)
        self.assertNotIn("extra_custom_attr", data)
        self.assertEqual(data["person_id"], "person:dad")
        self.assertEqual(data["group"], "parents")
        self.assertEqual(data["mode"], "active_mode")

        # Round trip
        deserialized = deserialize_ctx(serialized)
        self.assertEqual(deserialized["person_id"], "person:dad")

        # Raise value error for unknown key
        bad_json = json.dumps({"person_id": "dad", "illegal_attr": 42})
        with self.assertRaises(ValueError):
            deserialize_ctx(bad_json)

    async def test_shadow_blocks_before_hold(self):
        ctx = self._make_ctx(shadow=True)
        res = await self.gateway.execute("__test_hitl_write", {}, ctx)
        self.assertIn("[shadow: would have called __test_hitl_write", res)
        self.assertEqual(self.executed_count, 0)

        # Check no pending_hitl row created
        pending = await test_db.list_pending_hitl(status="pending")
        self.assertEqual(len(pending), 0)

    async def test_expire_stale_pending(self):
        ctx = self._make_ctx()
        hold_msg = await self.gateway.execute("__test_hitl_write", {}, ctx)
        pending_id = int(hold_msg.split("#")[1].split(")")[0])

        # Manually update expires_at to past in DB
        past_iso = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        async with test_db._db_conn() as db:
            await db.execute("UPDATE pending_hitl SET expires_at = ? WHERE id = ?", (past_iso, pending_id))
            await db.commit()

        # Run sweep
        await run_hitl_expiry_sweep(self.services)

        # Verify DB status is expired
        row = await test_db.get_pending_hitl(pending_id)
        self.assertEqual(row["status"], "expired")
        self.assertEqual(row["decided_by"], "system:expiry")

        # Verify activity log has expiration entry
        async with test_db._db_conn() as db:
            async with db.execute("SELECT * FROM activity_log WHERE event_type = 'hitl_expired'") as cur:
                log_row = await cur.fetchone()
                self.assertIsNotNone(log_row)
                meta_outer = json.loads(log_row["metadata"])
                meta_inner = json.loads(meta_outer["meta"])
                self.assertEqual(meta_inner["pending_id"], pending_id)

    async def test_deny_on_expired(self):
        ctx = self._make_ctx()
        hold_msg = await self.gateway.execute("__test_hitl_write", {}, ctx)
        pending_id = int(hold_msg.split("#")[1].split(")")[0])

        # Manually update expires_at to past in DB
        past_iso = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        async with test_db._db_conn() as db:
            await db.execute("UPDATE pending_hitl SET expires_at = ? WHERE id = ?", (past_iso, pending_id))
            await db.commit()

        # Denying should STILL succeed because denying doesn't have an expiry gate
        resolved = await test_db.resolve_pending_hitl(pending_id, "denied", "person:dad")
        self.assertTrue(resolved)

        row = await test_db.get_pending_hitl(pending_id)
        self.assertEqual(row["status"], "denied")
        self.assertEqual(row["decided_by"], "person:dad")

    async def test_set_pending_hitl_notify_message_ids(self):
        ctx = self._make_ctx()
        hold_msg = await self.gateway.execute("__test_hitl_write", {}, ctx)
        pending_id = int(hold_msg.split("#")[1].split(")")[0])

        await test_db.set_pending_hitl_notify_message_ids(pending_id, [1111, 2222])
        row = await test_db.get_pending_hitl(pending_id)
        self.assertEqual(row["notify_message_ids"], "[1111, 2222]")

        await test_db.set_pending_hitl_notify_message_ids(pending_id, [(101, 1111), (202, 2222)])
        row = await test_db.get_pending_hitl(pending_id)
        mapping = test_db.parse_pending_hitl_notify_map(row["notify_message_ids"])
        self.assertEqual(mapping, {101: 1111, 202: 2222})

    async def test_deny_pending(self):
        ctx = self._make_ctx()
        hold_msg = await self.gateway.execute("__test_hitl_write", {}, ctx)
        pending_id = int(hold_msg.split("#")[1].split(")")[0])

        ok = await deny_pending(pending_id, services=self.services, decided_by="person:dad")
        self.assertTrue(ok)
        row = await test_db.get_pending_hitl(pending_id)
        self.assertEqual(row["status"], "denied")

        ok2 = await deny_pending(pending_id, services=self.services, decided_by="person:dad")
        self.assertFalse(ok2)

    async def test_run_hitl_purge(self):
        pid = await test_db.create_pending_hitl(
            "tool", "{}", "{}", expires_at="2036-01-01T00:00:00Z", requested_at="2026-01-01T00:00:00Z",
        )
        await test_db.resolve_pending_hitl(pid, "approved", "admin")
        async with test_db._db_conn() as db:
            await db.execute(
                "UPDATE pending_hitl SET decided_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00Z", pid),
            )
            await db.commit()

        purged = await run_hitl_purge(services=self.services, older_than_days=7)
        self.assertEqual(purged, 1)
        self.assertIsNone(await test_db.get_pending_hitl(pid))

