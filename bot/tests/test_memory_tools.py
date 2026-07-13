import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as test_db


class TestMemoryTools(unittest.IsolatedAsyncioTestCase):
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

    async def test_search_activity_log(self):
        from tools.memory import handle_search_activity_log
        
        await test_db.log_activity(
            event_type="test",
            description="Checked Garmin sleep details",
            person_id="person:red"
        )

        res = await handle_search_activity_log({"query": "Garmin"}, self.ctx)
        self.assertIn("Checked Garmin sleep", res)

    async def test_search_activity_log_shadow_mode(self):
        from tools.memory import handle_search_activity_log
        self.ctx.shadow = True

        res = await handle_search_activity_log({"query": "Garmin"}, self.ctx)
        self.assertIn("[shadow: would have searched activity log for: Garmin ]", res)
