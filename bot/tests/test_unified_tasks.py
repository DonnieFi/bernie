import sys, os, tempfile, unittest
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as db

class SystemVirtualIdTest(unittest.TestCase):
    def test_is_system_virtual_task_id(self):
        self.assertTrue(db.is_system_virtual_task_id(db.system_virtual_task_id(1)))
        self.assertFalse(db.is_system_virtual_task_id(42))
        self.assertFalse(db.is_system_virtual_task_id(-500))


class UnifiedSchemaTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self._tmp.close()
        db.DB_PATH = self._tmp.name
        await db.init_db()
    async def asyncTearDown(self):
        os.unlink(self._tmp.name)
    async def _columns(self, table):
        async with db._db_conn() as c:
            cur = await c.execute(f"PRAGMA table_info({table})")
            return {r[1] for r in await cur.fetchall()}

    async def test_unified_tasks_columns(self):
        cols = await self._columns("unified_tasks")
        for needed in ("id","type","status","title","horizon","assigned_to","acceptable_assignees",
                       "visibility","priority","urgency","payload","current_run_id","workspace",
                       "is_recurring","due_at","requires_approval","approver_id","approved_at",
                       "completion_note","max_runtime_seconds","max_retries"):
            self.assertIn(needed, cols, needed)
    async def test_links_and_executions(self):
        self.assertEqual(await self._columns("task_links"), {"parent_id","child_id"})
        ex = await self._columns("task_executions")
        for needed in ("execution_id","task_id","status","started_at","completed_at","logs","metrics"):
            self.assertIn(needed, ex, needed)

class RowShapeTest(UnifiedSchemaTest):
    async def _insert(self, **over):
        cols = dict(type="chore", status="running", title="t", assigned_by="agent:bernie",
                    acceptable_assignees='["person:child2"]', visibility="family",
                    horizon="2026-05", priority="normal", created_at="2026-05-01T00:00:00Z")
        cols.update(over)
        keys = ",".join(cols); ph = ",".join("?"*len(cols))
        async with db._db_conn() as c:
            await c.execute(f"INSERT INTO unified_tasks ({keys}) VALUES ({ph})", tuple(cols.values()))
            await c.commit()
            cur = await c.execute("SELECT * FROM unified_tasks ORDER BY id DESC LIMIT 1")
            return db._row_to_task(await cur.fetchone())

    async def test_running_presents_as_legacy_pending_in_progress(self):
        t = await self._insert(status="running")
        self.assertEqual(t["status"], "pending"); self.assertTrue(t["in_progress"])
        self.assertEqual(t["kanban_status"], "running")
    async def test_done_with_approval_presents_as_legacy_approved(self):
        t = await self._insert(status="done", approved_at="2026-05-02T00:00:00Z")
        self.assertEqual(t["status"], "approved"); self.assertEqual(t["kanban_status"], "done")
    async def test_done_without_approval_presents_as_done(self):
        t = await self._insert(status="done")
        self.assertEqual(t["status"], "done")
    async def test_new_fields_exposed(self):
        t = await self._insert(type="research", visibility="internal")
        self.assertEqual(t["type"], "research"); self.assertEqual(t["visibility"], "internal")
        self.assertEqual(t["horizon"], "2026-05"); self.assertEqual(t["acceptable_assignees"], ["person:child2"])
    async def test_approver_id_exposed_under_legacy_key(self):
        t = await self._insert(approver_id="person:mom")
        self.assertEqual(t["approver_person_id"], "person:mom")

class CrudTest(UnifiedSchemaTest):
    async def test_create_defaults(self):
        t = await db.create_task(title="Recycling", assigned_to="person:child1",
                                  assigned_by="agent:bernie", due_at="2026-05-17T12:00:00Z",
                                  horizon="2026-07")
        self.assertEqual(t["type"], "chore"); self.assertEqual(t["kanban_status"], "todo")
        self.assertEqual(t["status"], "pending"); self.assertEqual(t["horizon"], "2026-07")
        self.assertEqual(t["acceptable_assignees"], ["person:child1"])
    async def test_get_and_list_round_trip(self):
        t = await db.create_task(title="A", assigned_to="person:child2", assigned_by="person:mom")
        self.assertEqual((await db.get_task(t["id"]))["title"], "A")
        self.assertEqual([x["id"] for x in await db.list_tasks_for_person("person:child2")], [t["id"]])
    async def test_list_pending_filter_excludes_done(self):
        a = await db.create_task(title="A", assigned_to="person:child2", assigned_by="b")
        b = await db.create_task(title="B", assigned_to="person:child2", assigned_by="b")
        await db.complete_task(b["id"])
        ids = [x["id"] for x in await db.list_tasks_for_person("person:child2", status="pending")]
        self.assertIn(a["id"], ids); self.assertNotIn(b["id"], ids)

    async def test_approved_filter_excludes_awaiting_approval(self):
        awaiting = await db.create_task(title="Await", assigned_to="person:child2", assigned_by="b",
                                          requires_approval=True, approver_person_id="person:mom")
        await db.complete_task(awaiting["id"])
        approved = await db.create_task(title="Ok", assigned_to="person:child2", assigned_by="b")
        await db.complete_task(approved["id"])
        await db.approve_task(approved["id"], approved=True)
        ids = [x["id"] for x in await db.list_all_tasks(status="approved")]
        self.assertNotIn(awaiting["id"], ids)
        self.assertIn(approved["id"], ids)
    async def test_update_allowed_field(self):
        t = await db.create_task(title="A", assigned_to="person:child2", assigned_by="b")
        upd = await db.update_task(t["id"], {"title": "B", "priority": "high"})
        self.assertEqual(upd["title"], "B"); self.assertEqual(upd["priority"], "high")

    async def test_update_task_horizon_persists(self):
        """S5: horizon is an allowed field so the board can move a task between months."""
        t = await db.create_task(title="A", assigned_to="person:child2", assigned_by="b")
        upd = await db.update_task(t["id"], {"horizon": "2026-07"})
        self.assertEqual(upd["horizon"], "2026-07")

class LifecycleTest(UnifiedSchemaTest):
    async def _mk(self, **kw):
        return await db.create_task(title="A", assigned_to="person:child2", assigned_by="b", **kw)
    async def test_complete_marks_done(self):
        t = await self._mk()
        d = await db.complete_task(t["id"], completion_note="ok")
        self.assertEqual(d["kanban_status"], "done"); self.assertEqual(d["status"], "done")
        self.assertEqual(d["completion_note"], "ok")
    async def test_due_lists_active_overdue_only(self):
        t = await self._mk(due_at="2026-05-01T00:00:00Z")
        await self._mk(due_at="2026-12-01T00:00:00Z")
        self.assertEqual([x["id"] for x in await db.list_due_tasks("2026-05-02T00:00:00Z")], [t["id"]])
        await db.complete_task(t["id"])
        self.assertEqual(await db.list_due_tasks("2026-05-02T00:00:00Z"), [])
    async def test_snooze_increments(self):
        t = await self._mk()
        self.assertEqual((await db.snooze_task(t["id"], "2026-05-20T00:00:00Z"))["snooze_count"], 1)
    async def test_approve_sets_approved_then_reject_returns_to_todo(self):
        t = await self._mk(requires_approval=True, approver_person_id="person:mom")
        await db.complete_task(t["id"])
        ap = await db.approve_task(t["id"], approved=True)
        self.assertEqual(ap["status"], "approved"); self.assertIsNotNone(ap["approved_at"])
        rj = await db.approve_task(t["id"], approved=False)
        self.assertEqual(rj["kanban_status"], "todo"); self.assertEqual(rj["status"], "pending")
    async def test_delete_removes_row(self):
        t = await self._mk(); await db.delete_task(t["id"])
        self.assertIsNone(await db.get_task(t["id"]))

    async def test_delete_cascades_executions(self):
        t = await self._mk()
        await db.start_execution(t["id"], "run-del-test")
        await db.delete_task(t["id"])
        self.assertEqual(await db.list_executions(t["id"]), [])

class LinksTest(UnifiedSchemaTest):
    async def _mk(self, status="todo"):
        t = await db.create_task(title="x", assigned_to="agent:bernie", assigned_by="agent:bernie")
        async with db._db_conn() as c:
            await c.execute("UPDATE unified_tasks SET status=? WHERE id=?", (status, t["id"])); await c.commit()
        return t["id"]
    async def test_link_then_cycle_rejected(self):
        a, b = await self._mk(), await self._mk()
        self.assertTrue(await db.link_tasks(a, b))
        self.assertFalse(await db.link_tasks(b, a))
    async def test_child_promoted_when_parents_done(self):
        parent, child = await self._mk("running"), await self._mk("todo")
        await db.link_tasks(parent, child)
        self.assertEqual(await db.promote_ready_tasks(), [])     # parent not done yet
        await db.complete_task(parent)
        self.assertIn(child, await db.promote_ready_tasks())
        self.assertEqual((await db.get_task(child))["kanban_status"], "ready")

    async def test_child_promoted_only_when_all_parents_done(self):
        """40B-2C coverage: exercises the NOT EXISTS 'all parents done' logic for >1 parent (N+1 fix)."""
        p1, p2, child = await self._mk("running"), await self._mk("running"), await self._mk("todo")
        await db.link_tasks(p1, child)
        await db.link_tasks(p2, child)
        await db.complete_task(p1)
        self.assertEqual(await db.promote_ready_tasks(), [])  # p2 still not done
        await db.complete_task(p2)
        promoted = await db.promote_ready_tasks()
        self.assertIn(child, promoted)
        self.assertEqual((await db.get_task(child))["kanban_status"], "ready")

class ExecTest(UnifiedSchemaTest):
    async def test_start_then_finish(self):
        t = await db.create_task(title="r", assigned_to="agent:bernie", assigned_by="agent:bernie")
        ex = await db.start_execution(t["id"], "run-a91f")
        self.assertEqual(ex["status"], "active")
        self.assertEqual((await db.get_task(t["id"]))["current_run_id"], "run-a91f")
        await db.finish_execution("run-a91f", status="completed", metrics={"tokens_in": 3100})
        rows = await db.list_executions(t["id"])
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["metrics"]["tokens_in"], 3100)

    async def test_start_execution_is_idempotent(self):
        t = await db.create_agent_task(type="research", title="r", assigned_by="agent:bernie")
        await db.start_execution(t["id"], "ct-9")
        await db.start_execution(t["id"], "ct-9")   # must NOT raise
        self.assertEqual(len(await db.list_executions(t["id"])), 1)


class AgentTaskTest(UnifiedSchemaTest):
    async def test_create_agent_task_defaults(self):
        t = await db.create_agent_task(
            type="research", title="Find a dentist", assigned_by="agent:bernie",
            assigned_to="agent:bernie", horizon="2026-05", details="near the new house")
        self.assertEqual(t["type"], "research")
        self.assertEqual(t["kanban_status"], "todo")
        self.assertEqual(t["assigned_to"], "agent:bernie")
        self.assertEqual(t["acceptable_assignees"], ["agent:bernie"])
        self.assertEqual(t["visibility"], "family")
    async def test_create_agent_task_code_is_internal(self):
        t = await db.create_agent_task(type="code", title="endpoint", assigned_by="agent:bernie",
                                       assigned_to="agent:nanobot", visibility="internal")
        self.assertEqual(t["visibility"], "internal")


class ReassignTest(UnifiedSchemaTest):
    async def test_reassign_changes_assignee(self):
        t = await db.create_task(title="A", assigned_to="person:child2", assigned_by="person:mom")
        upd = await db.reassign_task(t["id"], "person:dad")
        self.assertEqual(upd["assigned_to"], "person:dad")


class ReclaimerTest(UnifiedSchemaTest):
    async def test_reclaim_stale_running_task(self):
        """Reclaimer must detect a running task with a stale heartbeat and return it to ready,
        clear current_run_id, and record a 'reclaimed' task_event."""
        t = await db.create_agent_task(type="research", title="r", assigned_by="agent:bernie", assigned_to="agent:bernie")
        await db.start_execution(t["id"], "run-stale-1")
        old = (datetime.now(timezone.utc) - timedelta(minutes=100)).isoformat()
        async with db._db_conn() as c:
            await c.execute("UPDATE unified_tasks SET heartbeat=?, status='running' WHERE id=?", (old, t["id"]))
            await c.commit()
        stale = await db.get_stale_unified_runs(older_than_minutes=db.UNIFIED_RECLAIM_TIMEOUT_MINUTES)
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["id"], t["id"])
        reclaimed = await db.reclaim_stalled_unified_tasks(older_than_minutes=db.UNIFIED_RECLAIM_TIMEOUT_MINUTES)
        self.assertIn(t["id"], reclaimed)
        updated = await db.get_task(t["id"])
        self.assertEqual(updated["kanban_status"], "ready")
        self.assertIsNone(updated.get("current_run_id"))
        events = await db.list_task_events(t["id"])
        self.assertTrue(any(e.get("event_type") == "reclaimed" for e in events))

    async def test_fresh_running_without_heartbeat_not_stale(self):
        """Running tasks with no heartbeat yet must not be reclaimed until updated_at is old."""
        t = await db.create_agent_task(type="research", title="fresh", assigned_by="agent:bernie")
        await db.start_execution(t["id"], "run-fresh")
        stale = await db.get_stale_unified_runs(older_than_minutes=db.UNIFIED_RECLAIM_TIMEOUT_MINUTES)
        self.assertEqual(stale, [])


class SystemProjectionTest(UnifiedSchemaTest):
    async def test_cognitive_tasks_project_as_system_rows(self):
        now = datetime.now(timezone.utc).isoformat()
        async with db._db_conn() as c:
            await c.execute(
                "INSERT INTO cognitive_tasks (type, status, payload, run_at, created_at, heartbeat) "
                "VALUES ('reflection', 'active', '{}', ?, ?, ?)",
                (now, now, now),
            )
            await c.execute(
                "INSERT INTO cognitive_tasks (type, status, payload, run_at, created_at) "
                "VALUES ('consolidation', 'done', '{}', ?, ?)",
                (now, now),
            )
            await c.commit()
        rows = await db.list_cognitive_tasks_as_system()
        self.assertTrue(any(r["type"] == "system" and "reflection" in r["title"] for r in rows))
        self.assertTrue(any(r["kanban_status"] in ("running", "done") for r in rows))
        self.assertTrue(all(r["visibility"] == "internal" for r in rows))

    async def test_system_virtual_detail_resolves_negative_id(self):
        now = datetime.now(timezone.utc).isoformat()
        async with db._db_conn() as c:
            await c.execute(
                "INSERT INTO cognitive_tasks (type, status, payload, run_at, created_at, started_at, heartbeat) "
                "VALUES ('reflection', 'active', '{\"topic\":\"nightly reflection\"}', ?, ?, ?, ?)",
                (now, now, now, now),
            )
            cog_id = (await (await c.execute("SELECT last_insert_rowid()")).fetchone())[0]
            await c.commit()
        virtual_id = db.system_virtual_task_id(cog_id)
        detail = await db.get_system_task_detail(virtual_id)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["task"]["type"], "system")
        self.assertEqual(detail["task"]["id"], virtual_id)
        self.assertEqual(detail["parents"], [])
        self.assertEqual(len(detail["runs"]), 1)
        self.assertEqual(detail["runs"][0]["status"], "active")
