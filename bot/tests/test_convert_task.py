import sys, os, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as db


class ConvertTaskTypeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = self._tmp.name
        await db.init_db()

    async def asyncTearDown(self):
        os.unlink(self._tmp.name)

    async def test_chore_to_research_changes_type_and_visibility(self):
        t = await db.create_task(title="Vacuum", assigned_to="person:child2", assigned_by="person:mom")
        upd = await db.convert_task_type(t["id"], "research", assignee="agent:bernie")
        self.assertEqual(upd["type"], "research")
        self.assertEqual(upd["assigned_to"], "agent:bernie")
        self.assertEqual(upd["visibility"], "family")
        self.assertEqual(upd["acceptable_assignees"], ["agent:bernie"])

    async def test_chore_to_code_is_internal(self):
        t = await db.create_task(title="Fix script", assigned_to="person:dad", assigned_by="person:dad")
        upd = await db.convert_task_type(t["id"], "code", assignee="agent:nanobot")
        self.assertEqual(upd["type"], "code")
        self.assertEqual(upd["visibility"], "internal")

    async def test_research_to_chore_resets_done_lane(self):
        t = await db.create_agent_task(type="research", title="Topic", assigned_by="agent:bernie",
                                       assigned_to="agent:bernie", status="done")
        upd = await db.convert_task_type(t["id"], "chore", assignee="person:child1")
        self.assertEqual(upd["type"], "chore")
        self.assertEqual(upd["kanban_status"], "todo")
        self.assertEqual(upd["assigned_to"], "person:child1")

    async def test_system_type_rejected(self):
        async with db._db_conn() as c:
            await c.execute(
                """INSERT INTO unified_tasks (type, status, title, assigned_by, acceptable_assignees,
                   visibility, horizon, priority, created_at, updated_at)
                   VALUES ('system', 'todo', 'sys', 'agent:bernie', '[]', 'internal', 'someday', 'normal',
                           '2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z')"""
            )
            await c.commit()
            cur = await c.execute("SELECT id FROM unified_tasks ORDER BY id DESC LIMIT 1")
            tid = (await cur.fetchone())[0]
        with self.assertRaises(ValueError):
            await db.convert_task_type(tid, "chore", assignee="person:child2")

    async def test_reassign_updates_acceptable_assignees(self):
        t = await db.create_task(title="A", assigned_to="person:child2", assigned_by="person:mom")
        upd = await db.reassign_task(t["id"], "person:child1")
        self.assertEqual(upd["assigned_to"], "person:child1")
        self.assertEqual(upd["acceptable_assignees"], ["person:child1"])
