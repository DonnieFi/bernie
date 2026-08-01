import sys, os, unittest
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as test_db


class TestCognitiveTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        from tools import load_all_domains
        load_all_domains()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old_db_path = test_db.DB_PATH
        test_db.DB_PATH = self._tmp.name
        await test_db.init_db()

        class Ctx:
            shadow = False
            person_id = "person:red"
            group = "parents"
            config = {}
            class services:
                db = test_db
        self.ctx = Ctx()

    async def asyncTearDown(self):
        import os
        test_db.DB_PATH = self._old_db_path
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    async def test_request_research_enqueues_task(self):
        from tools.cognitive import handle_request_research
        args = {"topic": "Verify local Caddy config", "requester_id": "12345678901234567"}
        with patch("research_delivery_queue.register") as register:
            res = await handle_request_research(args, self.ctx)
        self.assertIn("Research task #", res)

        # Extract the task ID from response string
        # e.g., "Research task #123 queued..."
        import re
        match = re.search(r"task #(\d+)", res)
        self.assertIsNotNone(match)
        tid = int(match.group(1))

        # Let's verify the task is actually in database
        task = await test_db.get_cognitive_task(tid)
        self.assertIsNotNone(task)
        self.assertEqual(task["type"], "research")
        self.assertEqual(task["actor_id"], "person:red")
        self.assertEqual(task["channel_id"], "12345678901234567")
        self.assertEqual(task["payload"]["topic"], "Verify local Caddy config")
        self.assertEqual(task["payload"]["requester_id"], "12345678901234567")
        self.assertEqual(task["payload"]["delivery"], "dm")
        self.assertEqual(task["payload"]["depth"], 2)
        register.assert_called_once_with(
            "12345678901234567",
            tid,
            "Verify local Caddy config",
            default_delivery="dm",
        )
