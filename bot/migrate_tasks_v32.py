# bot/migrate_tasks_v32.py
"""One-shot: copy legacy `tasks` rows into `unified_tasks` (type='chore'). Idempotent.
Lives in bot/ so it imports as a top-level module inside the container."""
import asyncio, json
from datetime import datetime, timezone
import database as db
from task_status import to_unified_status, due_to_horizon

def _legacy_col(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
        return default

async def migrate() -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    copied = 0
    async with db._db_conn() as c:
        # Ensure the three Wave-A tables exist even on DBs initialized before this feature.
        # IF NOT EXISTS makes this safe when they're already present.
        await c.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                title          TEXT NOT NULL,
                details        TEXT,
                assigned_to    TEXT NOT NULL,
                assigned_by    TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'pending',
                in_progress    INTEGER NOT NULL DEFAULT 0,
                priority       TEXT NOT NULL DEFAULT 'normal',
                requires_approval INTEGER NOT NULL DEFAULT 0,
                approver_person_id TEXT,
                due_at         TEXT,
                snooze_until   TEXT,
                snooze_count   INTEGER NOT NULL DEFAULT 0,
                escalated_at   TEXT,
                last_prompted_at TEXT,
                remind_visibility TEXT NOT NULL DEFAULT 'private',
                remind_channel_id INTEGER,
                category       TEXT DEFAULT 'Task',
                is_recurring   INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT NOT NULL,
                updated_at     TEXT,
                completed_at   TEXT,
                approved_at    TEXT,
                completion_note TEXT
            );

            CREATE TABLE IF NOT EXISTS unified_tasks (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                type                TEXT NOT NULL DEFAULT 'chore',
                status              TEXT NOT NULL DEFAULT 'todo',
                title               TEXT NOT NULL,
                details             TEXT,
                horizon             TEXT,
                assigned_to         TEXT,
                assigned_by         TEXT NOT NULL,
                acceptable_assignees TEXT NOT NULL DEFAULT '[]',
                visibility          TEXT NOT NULL DEFAULT 'family',
                priority            TEXT NOT NULL DEFAULT 'normal',
                urgency             TEXT,
                is_recurring        INTEGER NOT NULL DEFAULT 0,
                due_at              TEXT,
                snooze_until        TEXT,
                snooze_count        INTEGER NOT NULL DEFAULT 0,
                escalated_at        TEXT,
                last_prompted_at    TEXT,
                remind_visibility   TEXT NOT NULL DEFAULT 'private',
                remind_channel_id   INTEGER,
                category            TEXT DEFAULT 'Task',
                requires_approval   INTEGER NOT NULL DEFAULT 0,
                approver_id         TEXT,
                approved_at         TEXT,
                completion_note     TEXT,
                payload             TEXT NOT NULL DEFAULT '{}',
                heartbeat           TEXT,
                error               TEXT,
                current_run_id      TEXT,
                max_runtime_seconds INTEGER,
                max_retries         INTEGER NOT NULL DEFAULT 0,
                workspace           TEXT,
                created_at          TEXT NOT NULL,
                updated_at          TEXT,
                completed_at        TEXT
            );

            CREATE TABLE IF NOT EXISTS task_links (
                parent_id INTEGER NOT NULL,
                child_id  INTEGER NOT NULL,
                PRIMARY KEY (parent_id, child_id)
            );

            CREATE TABLE IF NOT EXISTS task_executions (
                execution_id TEXT PRIMARY KEY,
                task_id      INTEGER NOT NULL,
                status       TEXT NOT NULL,
                started_at   TEXT NOT NULL,
                completed_at TEXT,
                logs         TEXT,
                metrics      TEXT
            );

            -- task_events is created in init_db, but that path is guarded on
            -- existing DBs. Create it here so the detail timeline works on prod.
            CREATE TABLE IF NOT EXISTS task_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id         INTEGER NOT NULL,
                event_type      TEXT NOT NULL,
                actor_person_id TEXT,
                metadata        TEXT,
                created_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id, created_at);

            -- All live reads now hit unified_tasks; mirror the old tasks indexes.
            CREATE INDEX IF NOT EXISTS idx_unified_assigned_to_status ON unified_tasks(assigned_to, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_unified_assigned_by_status ON unified_tasks(assigned_by, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_unified_due_pending        ON unified_tasks(status, due_at, snooze_until);
        """)
        cur = await c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        )
        if not await cur.fetchone():
            return 0
        cur = await c.execute("SELECT * FROM tasks")
        legacy = await cur.fetchall()
        cur = await c.execute("SELECT json_extract(payload,'$.migrated_from_task_id') FROM unified_tasks "
                              "WHERE json_extract(payload,'$.migrated_from_task_id') IS NOT NULL")
        already = {r[0] for r in await cur.fetchall()}
        for r in legacy:
            if r["id"] in already:
                continue
            status = to_unified_status(r["status"], bool(r["in_progress"]))
            horizon = due_to_horizon(r["due_at"]) if r["due_at"] else "someday"
            ins = await c.execute(
                """INSERT INTO unified_tasks
                   (type, status, title, details, horizon, assigned_to, assigned_by,
                    acceptable_assignees, visibility, priority, is_recurring, due_at, snooze_until,
                    snooze_count, escalated_at, last_prompted_at, remind_visibility, remind_channel_id,
                    category, requires_approval, approver_id, approved_at, completion_note,
                    payload, created_at, updated_at, completed_at)
                   VALUES ('chore', ?, ?, ?, ?, ?, ?, ?, 'family', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (status, r["title"], r["details"], horizon, r["assigned_to"], r["assigned_by"],
                 json.dumps([r["assigned_to"]]), r["priority"], r["is_recurring"], r["due_at"],
                 _legacy_col(r, "snooze_until"), int(_legacy_col(r, "snooze_count") or 0),
                 _legacy_col(r, "escalated_at"), _legacy_col(r, "last_prompted_at"),
                 _legacy_col(r, "remind_visibility") or "private", _legacy_col(r, "remind_channel_id"),
                 _legacy_col(r, "category") or "Task", r["requires_approval"],
                 _legacy_col(r, "approver_person_id"), _legacy_col(r, "approved_at"),
                 _legacy_col(r, "completion_note"),
                 json.dumps({"migrated_from_task_id": r["id"]}), r["created_at"], r["updated_at"],
                 r["completed_at"]))
            new_id = ins.lastrowid
            legacy_id = r["id"]
            cur_ev = await c.execute(
                "SELECT event_type, actor_person_id, metadata, created_at FROM task_events "
                "WHERE task_id=? ORDER BY created_at",
                (legacy_id,))
            events = await cur_ev.fetchall()
            for ev in events:
                await c.execute(
                    "INSERT INTO task_events (task_id, event_type, actor_person_id, metadata, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (new_id, ev["event_type"], ev["actor_person_id"], ev["metadata"], ev["created_at"]))
            if not events:
                await c.execute(
                    "INSERT INTO task_events (task_id, event_type, actor_person_id, metadata, created_at) "
                    "VALUES (?, 'created', ?, ?, ?)",
                    (new_id, r["assigned_by"],
                     json.dumps({"via": "migration", "migrated_from_task_id": legacy_id}),
                     r["created_at"] or now_iso))
            copied += 1
        await c.commit()
    return copied

if __name__ == "__main__":
    print(f"migrated {asyncio.run(migrate())} task(s)")
