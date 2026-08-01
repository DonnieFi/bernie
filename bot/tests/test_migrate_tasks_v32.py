# tests/test_migrate_tasks_v32.py
import sys, os, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as db
from migrate_tasks_v32 import migrate

class MigrateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self._tmp.close()
        db.DB_PATH = self._tmp.name
        await db.init_db()
        import sqlite3
        conn = sqlite3.connect(self._tmp.name)
        conn.executescript("""
            DROP TABLE IF EXISTS tasks;
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                details TEXT,
                assigned_to TEXT NOT NULL,
                assigned_by TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                in_progress INTEGER NOT NULL DEFAULT 0,
                priority TEXT NOT NULL DEFAULT 'normal',
                requires_approval INTEGER NOT NULL DEFAULT 0,
                approver_person_id TEXT,
                due_at TEXT,
                snooze_until TEXT,
                snooze_count INTEGER NOT NULL DEFAULT 0,
                escalated_at TEXT,
                last_prompted_at TEXT,
                remind_visibility TEXT NOT NULL DEFAULT 'private',
                remind_channel_id INTEGER,
                category TEXT DEFAULT 'Task',
                is_recurring INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                completed_at TEXT,
                approved_at TEXT,
                completion_note TEXT
            );
        """)
        conn.execute(
            """INSERT INTO tasks (title, details, assigned_to, assigned_by, status, in_progress,
               priority, requires_approval, approver_person_id, due_at, is_recurring, created_at)
               VALUES ('Recycling','blue bag','person:child1','agent:bernie','pending',1,'normal',
               1,'person:mom','2026-05-17T12:00:00Z',0,'2026-05-01T00:00:00Z')""")
        conn.commit()
        conn.close()
        if hasattr(db, "close_db"):
            await db.close_db()
    async def asyncTearDown(self):
        os.unlink(self._tmp.name)
    async def test_every_old_chore_round_trips(self):
        self.assertEqual(await migrate(), 1)
        rows = await db.list_all_tasks(); self.assertEqual(len(rows), 1)
        t = rows[0]
        self.assertEqual(t["type"], "chore"); self.assertEqual(t["kanban_status"], "running")
        self.assertEqual(t["horizon"], "2026-05"); self.assertEqual(t["approver_person_id"], "person:mom")
        self.assertEqual(t["acceptable_assignees"], ["person:child1"])
    async def test_idempotent(self):
        self.assertEqual(await migrate(), 1)
        self.assertEqual(await migrate(), 0)
        self.assertEqual(len(await db.list_all_tasks()), 1)

    async def test_migrate_copies_legacy_task_events(self):
        async with db._db_conn() as c:
            await c.execute(
                "INSERT INTO task_events (task_id, event_type, actor_person_id, metadata, created_at) "
                "VALUES (1, 'comment', 'person:mom', '{\"text\":\"old note\"}', '2026-05-01T00:00:00Z')")
            await c.commit()
        self.assertEqual(await migrate(), 1)
        t = (await db.list_all_tasks())[0]
        events = await db.list_task_events(t["id"])
        self.assertTrue(any(e["event_type"] == "comment" for e in events))
        comment = next(e for e in events if e["event_type"] == "comment")
        self.assertEqual(comment["metadata"]["text"], "old note")

    async def test_migrate_creates_tables_on_initialized_db_missing_them(self):
        # asyncSetUp already ran init_db() (creates unified tables) + seeded one legacy `tasks` row.
        # Simulate the prod DB that predates this feature: drop the three new tables.
        async with db._db_conn() as c:
            for t in ("unified_tasks", "task_links", "task_executions"):
                await c.execute(f"DROP TABLE IF EXISTS {t}")
            await c.commit()
        n = await migrate()                      # must (re)create tables, then copy
        self.assertEqual(n, 1)
        async with db._db_conn() as c:
            cur = await c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                                  "('unified_tasks','task_links','task_executions')")
            names = {r[0] for r in await cur.fetchall()}
        self.assertEqual(names, {"unified_tasks", "task_links", "task_executions"})
        self.assertEqual(len(await db.list_all_tasks()), 1)
