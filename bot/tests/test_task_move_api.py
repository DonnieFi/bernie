import sys, os, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as db


class SetKanbanStatus(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = self._tmp.name
        await db.init_db()

    async def asyncTearDown(self):
        os.unlink(self._tmp.name)

    async def test_set_kanban_status_valid(self):
        t = await db.create_agent_task(type="research", title="r", assigned_by="agent:bernie")
        upd = await db.set_kanban_status(t["id"], "running")
        self.assertEqual(upd["kanban_status"], "running")

    async def test_set_kanban_status_rejects_unknown(self):
        t = await db.create_agent_task(type="research", title="r", assigned_by="agent:bernie")
        with self.assertRaises(ValueError):
            await db.set_kanban_status(t["id"], "bogus")

    async def test_set_kanban_status_all_valid_statuses(self):
        """Each valid status round-trips through set_kanban_status."""
        from task_status import UNIFIED_STATUSES
        t = await db.create_agent_task(type="research", title="all-statuses", assigned_by="agent:bernie")
        for s in UNIFIED_STATUSES:
            upd = await db.set_kanban_status(t["id"], s)
            self.assertEqual(upd["kanban_status"], s, f"Expected kanban_status={s}")
