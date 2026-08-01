import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone as dt_timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import database as db
except ModuleNotFoundError:
    db = None


@unittest.skipUnless(db is not None, "database module not available")
class TestPhase13DatabaseOps(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "phase13_test.db")
        await db.init_db()

    async def asyncTearDown(self):
        if hasattr(db, 'close_db'):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_task_create_and_delete(self):
        created = await db.create_task(
            title="Empty dishwasher",
            assigned_to="child1",
            assigned_by="dad",
            details="Kitchen cleanup",
            requires_approval=True,
            approver_person_id="dad",
            remind_visibility="private",
        )

        self.assertIsNotNone(created)
        self.assertEqual(created["title"], "Empty dishwasher")
        self.assertEqual(created["assigned_to"], "child1")
        self.assertTrue(created["requires_approval"])

        fetched = await db.get_task(created["id"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["id"], created["id"])

        await db.delete_task(created["id"])
        deleted = await db.get_task(created["id"])
        self.assertIsNone(deleted)

    async def test_task_list_scoped_by_person(self):
        await db.create_task(title="Task A", assigned_to="child1", assigned_by="dad")
        await db.create_task(title="Task B", assigned_to="dad", assigned_by="dad")

        child1_tasks = await db.list_tasks_for_person("child1", status="all", include_assigned_by=True)
        self.assertEqual(len(child1_tasks), 1)
        self.assertEqual(child1_tasks[0]["assigned_to"], "child1")

    async def test_automation_create_and_delete(self):
        run_at = (datetime.now(dt_timezone.utc) + timedelta(hours=2)).isoformat()
        created = await db.create_automation(
            title="Bin reminder",
            message="Take bins out",
            person_id="dad",
            schedule_kind="once",
            schedule_payload={"run_at": run_at},
            timezone="America/Halifax",
            created_by="dad",
            audience_scope="everyone",
            next_run_at=run_at,
        )

        self.assertIsNotNone(created)
        self.assertEqual(created["title"], "Bin reminder")
        self.assertEqual(created["schedule_kind"], "once")
        self.assertEqual(created["audience_scope"], "everyone")

        fetched = await db.get_automation(created["id"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["id"], created["id"])

        await db.delete_automation(created["id"])
        deleted = await db.get_automation(created["id"])
        self.assertIsNone(deleted)


    # ── update_task tests ──────────────────────────────────────────────────

    async def test_update_task_allowed_fields(self):
        """update_task should persist title, details, priority, in_progress, category."""
        task = await db.create_task(title="Vacuum", assigned_to="child1", assigned_by="dad")
        updated = await db.update_task(task["id"], {
            "title": "Vacuum upstairs",
            "details": "Include hallway",
            "priority": "high",
            "in_progress": True,
            "category": "Chores",
        })
        self.assertEqual(updated["title"], "Vacuum upstairs")
        self.assertEqual(updated["details"], "Include hallway")
        self.assertEqual(updated["priority"], "high")
        self.assertTrue(updated["in_progress"])
        self.assertEqual(updated["category"], "Chores")

    async def test_update_task_blocks_status_change(self):
        """update_task must NOT allow status changes — approval flow must be used instead."""
        task = await db.create_task(title="Mow lawn", assigned_to="child1", assigned_by="dad")
        result = await db.update_task(task["id"], {"status": "done"})
        # status should remain pending; the blocked field is silently ignored
        self.assertEqual(result["status"], "pending")

    async def test_update_task_blocks_reassignment(self):
        """update_task must NOT allow changing assigned_to."""
        task = await db.create_task(title="Dishes", assigned_to="child1", assigned_by="dad")
        result = await db.update_task(task["id"], {"assigned_to": "mom"})
        self.assertEqual(result["assigned_to"], "child1")

    async def test_update_task_empty_is_noop(self):
        """Calling update_task with no recognised fields should return the task unchanged."""
        task = await db.create_task(title="Walk dog", assigned_to="child2", assigned_by="dad")
        result = await db.update_task(task["id"], {"nonexistent_field": "boom"})
        self.assertEqual(result["title"], "Walk dog")

    async def test_task_new_columns_defaults(self):
        """New columns (priority, in_progress, is_recurring, category) should have correct defaults."""
        task = await db.create_task(
            title="Buy groceries",
            assigned_to="dad",
            assigned_by="dad",
        )
        self.assertEqual(task["priority"], "normal")
        self.assertFalse(task["in_progress"])
        self.assertFalse(task["is_recurring"])
        self.assertEqual(task["category"], "Task")

    async def test_task_new_columns_on_create(self):
        """New columns should be settable at creation time."""
        task = await db.create_task(
            title="Weekly review",
            assigned_to="dad",
            assigned_by="dad",
            priority="low",
            in_progress=False,
            is_recurring=True,
            category="Admin",
        )
        self.assertEqual(task["priority"], "low")
        self.assertTrue(task["is_recurring"])
        self.assertEqual(task["category"], "Admin")

    async def test_vacuum_db_closes_singleton_and_succeeds(self):
        """Weekly VACUUM must close the shared connection first — otherwise
        SQLite reports 'cannot VACUUM - SQL statements in progress'."""
        await db.create_task(title="vacuum probe", assigned_to="dad", assigned_by="dad")
        # Warm the singleton so vacuum_db must explicitly close it.
        async with db.db_conn() as conn:
            await conn.execute("SELECT 1")
        self.assertIsNotNone(db._conn)

        await db.vacuum_db()

        self.assertIsNone(db._conn)
        async with db.db_conn() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM unified_tasks")
            row = await cur.fetchone()
            self.assertGreaterEqual(row[0], 1)

    async def test_vacuum_db_records_last_vacuum_at(self):
        await db.vacuum_db()
        ts = await db.get_db_metadata("last_vacuum_at")
        self.assertIsNotNone(ts)
        self.assertTrue(ts.endswith("Z"))


if __name__ == "__main__":
    unittest.main()
