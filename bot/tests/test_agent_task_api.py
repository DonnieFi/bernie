"""Task 2 — S4: exercises research_bridge.enqueue_for_unified end-to-end at the DB level.

Asserts:
  (a) a cognitive_tasks row is enqueued carrying unified_task_id + delivery="board"
  (b) the unified_tasks row flips to running with a current_run_id
  (c) list_executions(task_id) has an active run row
"""
import sys, os, tempfile, unittest, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as db


class EnqueueForUnified(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = self._tmp.name
        await db.init_db()

    async def asyncTearDown(self):
        os.unlink(self._tmp.name)

    async def test_enqueue_creates_cognitive_row_with_board_delivery(self):
        """cognitive_tasks row must carry unified_task_id + delivery='board'."""
        from research_bridge import enqueue_for_unified
        t = await db.create_agent_task(type="research", title="Find a dentist",
                                       assigned_by="agent:bernie", assigned_to="agent:bernie")
        await enqueue_for_unified(t["id"], "Find a dentist", actor_id="dad")

        async with db._db_conn() as c:
            cur = await c.execute("SELECT payload FROM cognitive_tasks WHERE type='research' ORDER BY id DESC LIMIT 1")
            row = await cur.fetchone()
        self.assertIsNotNone(row, "cognitive_tasks row must exist")
        payload = json.loads(row["payload"])
        self.assertEqual(payload["unified_task_id"], t["id"],
                         "payload must carry the unified task id")
        self.assertEqual(payload["delivery"], "board",
                         "payload must set delivery='board'")

    async def test_enqueue_flips_unified_task_to_running(self):
        """unified_tasks row must become running with a current_run_id after enqueue."""
        from research_bridge import enqueue_for_unified
        t = await db.create_agent_task(type="research", title="Dentist lookup",
                                       assigned_by="agent:bernie", assigned_to="agent:bernie")
        await enqueue_for_unified(t["id"], "Dentist lookup")

        updated = await db.get_task(t["id"])
        self.assertEqual(updated["kanban_status"], "running",
                         "unified task must be running after enqueue")
        self.assertIsNotNone(updated["current_run_id"],
                             "current_run_id must be set after enqueue")

    async def test_enqueue_creates_active_execution_row(self):
        """list_executions must return at least one active row after enqueue."""
        from research_bridge import enqueue_for_unified
        t = await db.create_agent_task(type="research", title="Schedule lookup",
                                       assigned_by="agent:bernie", assigned_to="agent:bernie")
        await enqueue_for_unified(t["id"], "Schedule lookup", actor_id="")

        runs = await db.list_executions(t["id"])
        self.assertTrue(len(runs) >= 1, "at least one execution row required")
        active = [r for r in runs if r["status"] == "active"]
        self.assertTrue(len(active) >= 1, "at least one run row must be active")
