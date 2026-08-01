import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as test_db


class TestIdentityTools(unittest.IsolatedAsyncioTestCase):
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
            group = "admin"
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

    async def test_get_unresolved_entities(self):
        from identity_service import IdentityService
        from tools.identity import handle_get_unresolved_entities

        svc = IdentityService()
        mac = "AA:BB:CC:DD:EE:FF"
        for _ in range(5):
            await svc.log_unresolved_entity(mac, "MAC", {"essid": "Bernie-LAN"})

        res = await handle_get_unresolved_entities({"min_count": 2, "limit": 10}, self.ctx)
        self.assertIn("Unresolved entities", res)
        self.assertIn("AA:BB:CC:DD:EE:FF", res)
        self.assertIn("seen 5x", res)
