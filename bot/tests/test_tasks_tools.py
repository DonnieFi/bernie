import sys, os, unittest
from unittest.mock import AsyncMock, MagicMock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as test_db


class TestTasksTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        from tools import load_all_domains
        load_all_domains()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old_db_path = test_db.DB_PATH
        test_db.DB_PATH = self._tmp.name
        await test_db.init_db()
        from db_binding import bind_database
        bind_database(test_db)

        self.orchestrator_mock = MagicMock()
        self.orchestrator_mock.notify = AsyncMock()

        def _notification(**kwargs):
            note = MagicMock()
            for key, val in kwargs.items():
                setattr(note, key, val)
            return note

        self.orchestrator_mock.notification = _notification

        from store.task_store import SQLiteTaskStore
        from services.unified_task_service import UnifiedTaskService
        from constants import registry as default_registry
        default_registry.load({
            "family_members": {
                "Dad": {
                    "canonical_id": "dad",
                    "first_name": "Dad",
                    "role": "parent"
                },
                "Red": {
                    "canonical_id": "red",
                    "first_name": "Red",
                    "role": "parent"
                }
            }
        })
        t_store = SQLiteTaskStore()
        u_tasks = UnifiedTaskService(
            task_store=t_store,
            person_registry=default_registry,
            config={"task_types": {
                "research": ["agent:bernie"],
                "bernie": ["agent:bernie"],
                "code": ["agent:nanobot"],
                "chore": ["person:*"],
            }},
            notification_router=self.orchestrator_mock,
        )

        class Ctx:
            shadow = False
            person_id = "person:red"
            group = "parents"
            config = {}
            class services:
                db = test_db
                task_store = t_store
                orchestrator = self.orchestrator_mock
                unified_tasks = u_tasks
                automation_store = None  # allow automation tools in mapped test runs (tasks.py falls back to db)
        self.ctx = Ctx()

    async def asyncTearDown(self):
        import os
        test_db.DB_PATH = self._old_db_path
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    async def test_create_and_list_tasks(self):
        from tools.tasks import handle_create_task, handle_list_tasks
        
        # Create a task for Dad
        create_args = {
            "title": "Clean room",
            "details": "Under the bed too",
            "assigned_to": "dad"
        }
        res_create = await handle_create_task(create_args, self.ctx)
        self.assertIn("Task #", res_create)

        # List tasks for Dad
        list_args = {
            "person": "dad",
            "status": "pending"
        }
        res_list = await handle_list_tasks(list_args, self.ctx)
        self.assertIn("Clean room", res_list)
        self.assertIn("Assigned to: dad", res_list)

    async def test_complete_task_by_title(self):
        from tools.tasks import handle_create_task, handle_complete_task
        # Create task
        create_args = {
            "title": "Water plants",
            "assigned_to": "dad"
        }
        await handle_create_task(create_args, self.ctx)

        # Complete task by title
        comp_args = {
            "title": "Water plants",
            "note": "Done in morning"
        }
        res_comp = await handle_complete_task(comp_args, self.ctx)
        self.assertTrue("completed and closed" in res_comp or "parental approval" in res_comp)

    async def test_delete_task(self):
        from tools.tasks import handle_create_task, handle_delete_task, handle_list_tasks
        # Create task
        create_args = {
            "title": "Mow lawn",
            "assigned_to": "dad"
        }
        res_create = await handle_create_task(create_args, self.ctx)
        import re
        tid = int(re.search(r"Task #(\d+)", res_create).group(1))

        # Delete task
        res_del = await handle_delete_task({"task_id": tid}, self.ctx)
        self.assertIn("deleted permanently", res_del)

        # List tasks
        res_list = await handle_list_tasks({"person": "dad"}, self.ctx)
        self.assertIn("No pending tasks found for dad", res_list)

    async def test_create_and_list_automation(self):
        from tools.tasks import handle_create_automation, handle_list_automations
        # Create daily automation
        create_args = {
            "title": "Daily speed test",
            "message": "Time to test LAN speed",
            "schedule_kind": "daily",
            "schedule": "08:00",
            "audience": "self"
        }
        res_create = await handle_create_automation(create_args, self.ctx)
        self.assertIn("Automation #", res_create)

        # List automations
        res_list = await handle_list_automations({}, self.ctx)
        self.assertIn("Daily speed test", res_list)
        self.assertIn("daily", res_list)

    async def test_shadow_create_task_no_db_write(self):
        from tools.tasks import handle_create_task, handle_list_tasks
        self.ctx.shadow = True

        # Create task in shadow mode
        create_args = {
            "title": "Do laundry",
            "assigned_to": "dad"
        }
        res_create = await handle_create_task(create_args, self.ctx)
        self.assertIn("[shadow: would have called create_task", res_create)

        # List tasks under non-shadow context
        self.ctx.shadow = False
        res_list = await handle_list_tasks({"person": "dad"}, self.ctx)
        self.assertIn("No pending tasks found for dad", res_list)
