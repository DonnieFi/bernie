import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store.task_store import InMemoryTaskStore

class TestTaskStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = InMemoryTaskStore()

    async def test_create_and_get_task(self):
        t = await self.store.create_agent_task(
            type="research", title="foo", details="bar", assigned_to="agent:bernie",
            assigned_by="user:test", priority="high", horizon=None, visibility="internal"
        )
        self.assertIsNotNone(t["id"])
        
        fetched = await self.store.get_task(t["id"])
        self.assertEqual(fetched["title"], "foo")
        self.assertEqual(fetched["kanban_status"], "todo")

    async def test_execution_lifecycle(self):
        t = await self.store.create_agent_task(
            type="code", title="foo", details="bar", assigned_to="agent:nanobot",
            assigned_by="user:test", priority="high", horizon=None, visibility="internal"
        )
        await self.store.start_execution(t["id"], "run-1")
        runs = await self.store.list_executions(t["id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "running")

        await self.store.finish_execution("run-1", "completed", "done")
        runs = await self.store.list_executions(t["id"])
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(runs[0]["logs"], "done")

if __name__ == "__main__":
    unittest.main()
