"""Task 3 — GET /api/tasks/{id}/detail
Tests:
  1. db.list_task_events  (Step 3)
  2. db.get_task_links    (W3 fix)
  3. Composition: detail parts assemble correctly
"""
import sys, os, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as db


class TaskDetailParts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = self._tmp.name
        await db.init_db()

    async def asyncTearDown(self):
        os.unlink(self._tmp.name)

    # ── list_task_events ──────────────────────────────────────────────────────
    async def test_list_task_events_returns_comment(self):
        t = await db.create_agent_task(
            type="research", title="r",
            assigned_by="agent:bernie", assigned_to="agent:bernie"
        )
        await db.add_task_event(t["id"], "comment", "agent:bernie", {"text": "hi"})
        events = await db.list_task_events(t["id"])
        self.assertTrue(any(e["event_type"] == "comment" for e in events))
        comment = next(e for e in events if e["event_type"] == "comment")
        self.assertEqual(comment["metadata"]["text"], "hi")

    async def test_list_task_events_has_auto_created_event(self):
        # create_agent_task emits a "created" event automatically
        t = await db.create_agent_task(
            type="research", title="auto-created",
            assigned_by="agent:bernie"
        )
        events = await db.list_task_events(t["id"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "created")

    async def test_list_task_events_ordered_asc(self):
        t = await db.create_agent_task(
            type="research", title="order-test",
            assigned_by="agent:bernie"
        )
        # create_agent_task already emitted a "created" event; add a comment after
        await db.add_task_event(t["id"], "comment", "agent:bernie", {"text": "second"})
        events = await db.list_task_events(t["id"])
        self.assertEqual(events[0]["event_type"], "created")
        self.assertEqual(events[-1]["event_type"], "comment")

    # ── get_task_links ────────────────────────────────────────────────────────
    async def test_get_task_links_no_links(self):
        t = await db.create_agent_task(
            type="research", title="isolated",
            assigned_by="agent:bernie"
        )
        links = await db.get_task_links(t["id"])
        self.assertEqual(links, {"parents": [], "children": []})

    async def test_get_task_links_returns_child_ids(self):
        parent = await db.create_agent_task(
            type="research", title="parent",
            assigned_by="agent:bernie"
        )
        child = await db.create_agent_task(
            type="research", title="child",
            assigned_by="agent:bernie"
        )
        await db.link_tasks(parent["id"], child["id"])
        links = await db.get_task_links(parent["id"])
        self.assertIn(child["id"], links["children"])
        self.assertEqual(links["parents"], [])

    async def test_get_task_links_returns_parent_ids(self):
        parent = await db.create_agent_task(
            type="research", title="parent2",
            assigned_by="agent:bernie"
        )
        child = await db.create_agent_task(
            type="research", title="child2",
            assigned_by="agent:bernie"
        )
        await db.link_tasks(parent["id"], child["id"])
        links = await db.get_task_links(child["id"])
        self.assertIn(parent["id"], links["parents"])
        self.assertEqual(links["children"], [])

    # ── composition: all detail parts together ────────────────────────────────
    async def test_detail_parts_compose(self):
        """Drive a real task with a run + comment event + parent/child link."""
        parent = await db.create_agent_task(
            type="research", title="parent-task",
            assigned_by="agent:bernie"
        )
        t = await db.create_agent_task(
            type="research", title="r",
            assigned_by="agent:bernie", assigned_to="agent:bernie"
        )
        await db.link_tasks(parent["id"], t["id"])

        await db.start_execution(t["id"], "run-1")
        await db.finish_execution("run-1", status="completed")
        await db.add_task_event(t["id"], "comment", "agent:bernie", {"text": "hi"})

        runs = await db.list_executions(t["id"])
        events = await db.list_task_events(t["id"])
        links = await db.get_task_links(t["id"])

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "completed")
        self.assertTrue(any(e["event_type"] == "comment" for e in events))
        self.assertIn(parent["id"], links["parents"])
        self.assertEqual(links["children"], [])
