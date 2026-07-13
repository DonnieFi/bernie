"""database.tasks — domain module (8lx.1 Phase 1)."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import sqlite_async

from database.conn import (
    HFX,
    _db_conn,
    _get_connection,
    _get_init_lock,
    _get_lock,
    _pkg,
    _resolve_db_path,
    close_db,
    db_conn,
    wal_checkpoint_passive,
)

log = logging.getLogger("database.tasks")

SYSTEM_TASK_ID_OFFSET = 1_000_000

UNIFIED_RECLAIM_TIMEOUT_MINUTES = 90

def is_system_virtual_task_id(task_id: int) -> bool:
    return task_id <= -SYSTEM_TASK_ID_OFFSET

def system_virtual_task_id(cognitive_id: int) -> int:
    return -SYSTEM_TASK_ID_OFFSET - cognitive_id

def cognitive_id_from_system_virtual(task_id: int) -> int | None:
    if task_id > -SYSTEM_TASK_ID_OFFSET:
        return None
    return -task_id - SYSTEM_TASK_ID_OFFSET

def _system_task_row_from_cognitive(d: dict, *, now: str | None = None) -> dict:
    """Shape one cognitive_tasks row as a read-only system card for the board."""
    import json as _json
    now = now or datetime.now(dt_timezone.utc).isoformat()
    status_map = {"active": "running", "queued": "ready", "done": "done"}
    payload = d.get("payload")
    if isinstance(payload, str):
        try:
            payload = _json.loads(payload or "{}")
        except (ValueError, TypeError):
            payload = {}
    elif not isinstance(payload, dict):
        payload = {}
    title = payload.get("topic") or payload.get("name") or f"{d['type']} worker"
    created = d.get("created_at") or now
    cog_id = d["id"]
    kanban = status_map.get(d["status"], "running")
    return {
        "id": system_virtual_task_id(cog_id),
        "type": "system",
        "status": "pending",
        "kanban_status": kanban,
        "title": str(title)[:80],
        "details": f"cognitive worker · {d['type']}",
        "assigned_to": "agent:research-worker",
        "assigned_by": "agent:bernie",
        "acceptable_assignees": ["agent:research-worker"],
        "visibility": "internal",
        "priority": "low",
        "horizon": created[:7] if len(created) >= 7 else "someday",
        "created_at": created,
        "updated_at": d.get("heartbeat") or d.get("completed_at") or created,
        "error": d.get("error"),
        "current_run_id": None,
        "system_kind": d["type"],
        "in_progress": d["status"] == "active",
        "blocked_reason": d.get("error") if kanban == "blocked" else None,
    }

def _row_to_task(row: sqlite3.Row) -> dict:
    import json as _json
    from task_status import to_legacy_status
    kanban_status = row["status"]
    legacy_status, in_progress = to_legacy_status(kanban_status)
    if kanban_status == "done" and row["approved_at"]:
        legacy_status = "approved"          # back-compat: done + approved_at == legacy 'approved'
    try:
        acceptable = _json.loads(row["acceptable_assignees"] or "[]")
    except (ValueError, TypeError):
        acceptable = []
    return {
        "id": row["id"],
        "title": row["title"],
        "details": row["details"] or "",
        "assigned_to": row["assigned_to"],
        "assigned_by": row["assigned_by"],
        "status": legacy_status,
        "in_progress": in_progress,
        "requires_approval": bool(row["requires_approval"]),
        "approver_person_id": row["approver_id"],
        "due_at": row["due_at"],
        "snooze_until": row["snooze_until"],
        "snooze_count": int(row["snooze_count"] or 0),
        "escalated_at": row["escalated_at"],
        "last_prompted_at": row["last_prompted_at"],
        "remind_visibility": row["remind_visibility"] or "private",
        "remind_channel_id": row["remind_channel_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "approved_at": row["approved_at"],
        "completion_note": row["completion_note"] or "",
        "category": row["category"] or "Task",
        "is_recurring": bool(row["is_recurring"]),
        "priority": row["priority"] or "normal",
        # new unified fields
        "type": row["type"],
        "kanban_status": kanban_status,
        "horizon": row["horizon"],
        "visibility": row["visibility"] or "family",
        "urgency": row["urgency"],
        "acceptable_assignees": acceptable,
        "workspace": row["workspace"],
        "current_run_id": row["current_run_id"],
    }

async def create_task(
    title: str,
    assigned_to: str,
    assigned_by: str,
    details: str = "",
    due_at: str | None = None,
    requires_approval: bool = False,
    approver_person_id: str | None = None,
    remind_visibility: str = "private",
    remind_channel_id: int | None = None,
    category: str = "Task",
    is_recurring: bool = False,
    in_progress: bool = False,
    priority: str = "normal",
    horizon: str | None = None,
) -> dict:
    import json as _json
    from task_status import to_unified_status, due_to_horizon
    now = datetime.now(dt_timezone.utc).isoformat()
    remind_visibility = remind_visibility if remind_visibility in {"private", "channel"} else "private"
    status = to_unified_status("pending", in_progress)
    horizon = horizon or due_to_horizon(due_at)   # explicit month wins; else derive from due date
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO unified_tasks
               (type, status, title, details, horizon, assigned_to, assigned_by,
                acceptable_assignees, visibility, priority, is_recurring, due_at,
                snooze_until, snooze_count, remind_visibility, remind_channel_id,
                category, requires_approval, approver_id, created_at, updated_at)
               VALUES ('chore', ?, ?, ?, ?, ?, ?, ?, 'family', ?, ?, ?, NULL, 0, ?, ?, ?, ?, ?, ?, ?)""",
            (status, title, details or "", horizon, assigned_to, assigned_by,
             _json.dumps([assigned_to]), priority, int(bool(is_recurring)), due_at,
             remind_visibility, remind_channel_id, category, int(bool(requires_approval)),
             approver_person_id, now, now),
        )
        cur = await db.execute("SELECT last_insert_rowid()")
        task_id = int((await cur.fetchone())[0]); await db.commit()
    await add_task_event(task_id, "created", assigned_by, {
        "assigned_to": assigned_to, "requires_approval": bool(requires_approval),
        "due_at": due_at, "remind_visibility": remind_visibility})
    return await get_task(task_id)

async def create_agent_task(type: str, title: str, assigned_by: str,
                            assigned_to: str | None = None, details: str = "",
                            horizon: str | None = None, priority: str = "normal",
                            visibility: str = "family", acceptable_assignees: list | None = None,
                            payload: dict | None = None, status: str = "todo") -> dict:
    import json as _json
    now = datetime.now(dt_timezone.utc).isoformat()
    accept = acceptable_assignees if acceptable_assignees is not None else ([assigned_to] if assigned_to else [])
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO unified_tasks
               (type, status, title, details, horizon, assigned_to, assigned_by,
                acceptable_assignees, visibility, priority, payload, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (type, status, title, details or "", horizon, assigned_to, assigned_by,
             _json.dumps(accept), visibility, priority, _json.dumps(payload or {}), now, now))
        cur = await db.execute("SELECT last_insert_rowid()")
        task_id = int((await cur.fetchone())[0]); await db.commit()
    await add_task_event(task_id, "created", assigned_by, {"type": type, "assigned_to": assigned_to})
    return await get_task(task_id)

async def convert_task_type(task_id: int, new_type: str, *, assignee: str | None = None) -> dict | None:
    """Change a task's type (chore/research/bernie/code). Clears chore-approval state when leaving chore."""
    import json as _json
    task = await get_task(task_id)
    if not task:
        return None
    old_type = task.get("type") or "chore"
    if old_type == "system" or new_type == "system":
        raise ValueError("system type cannot be converted")
    allowed = {"chore", "research", "bernie", "code"}
    if new_type not in allowed:
        raise ValueError(f"invalid type: {new_type!r}")
    if old_type == new_type:
        return task

    new_assignee = assignee if assignee is not None else task.get("assigned_to")
    visibility = "internal" if new_type == "code" else "family"
    accept = _json.dumps([new_assignee] if new_assignee else [])
    kanban = task.get("kanban_status") or "todo"
    if kanban in ("done", "archived"):
        kanban = "todo"

    now = datetime.now(dt_timezone.utc).isoformat()
    clears = []
    if old_type == "chore" and new_type != "chore":
        clears = ["requires_approval=0", "approver_id=NULL", "approved_at=NULL",
                  "completion_note=''", "completed_at=NULL"]
    elif new_type == "chore":
        clears = ["requires_approval=0", "approver_id=NULL", "approved_at=NULL"]

    set_parts = ["type=?", "assigned_to=?", "acceptable_assignees=?", "visibility=?",
                 "status=?", "updated_at=?"] + clears
    values = [new_type, new_assignee, accept, visibility, kanban, now]

    async with _db_conn() as db:
        await db.execute(
            f"UPDATE unified_tasks SET {', '.join(set_parts)} WHERE id=?",
            values + [task_id],
        )
        await db.commit()
    return await get_task(task_id)

async def update_task(task_id: int, updates: dict) -> dict | None:
    """Generic update helper for Kanban and other task changes."""
    if not updates:
        return await get_task(task_id)
    
    now = datetime.now(dt_timezone.utc).isoformat()
    # "status" and "assigned_to" are intentionally excluded:
    # status changes must go through complete/approve/snooze endpoints (to trigger notifications),
    # and reassignment via a generic PATCH would bypass ownership checks.
    allowed_fields = {"title", "details", "in_progress", "priority", "category", "is_recurring", "due_at", "horizon"}
    
    # Filter and normalize
    safe_updates = {}
    for k, v in updates.items():
        if k not in allowed_fields:
            continue
        if k == "in_progress":
            # unified_tasks has no in_progress column — map to kanban status
            safe_updates["status"] = "running" if v else "todo"
        elif k == "is_recurring":
            safe_updates[k] = int(bool(v))
        else:
            safe_updates[k] = v
            
    if not safe_updates:
        return await get_task(task_id)
        
    set_clause = ", ".join([f"{k} = ?" for k in safe_updates.keys()])
    set_clause += ", updated_at = ?"
    values = list(safe_updates.values()) + [now, task_id]
    
    async with _db_conn() as db:
        await db.execute(f"UPDATE unified_tasks SET {set_clause} WHERE id = ?", values)
        await db.commit()

    return await get_task(task_id)

async def set_kanban_status(task_id: int, status: str) -> dict | None:
    """Set the kanban_status column directly. Callers must route 'done' through
    complete_task (which fires notify/approval); this helper handles all other
    lane moves (triage/todo/ready/running/blocked/archived)."""
    from task_status import UNIFIED_STATUSES
    if status not in UNIFIED_STATUSES:
        raise ValueError(f"invalid kanban status: {status!r}")
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            "UPDATE unified_tasks SET status=?, updated_at=? WHERE id=?",
            (status, now, task_id),
        )
        await db.commit()
    return await get_task(task_id)

async def update_unified_task_heartbeat(task_id: int) -> None:
    """Update the heartbeat timestamp for a running unified task."""
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            "UPDATE unified_tasks SET heartbeat=?, updated_at=? WHERE id=?",
            (now, now, task_id)
        )
        await db.commit()

async def block_task(task_id: int, reason: str) -> dict | None:
    """Set status to 'blocked' and record the error reason."""
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            "UPDATE unified_tasks SET status='blocked', error=?, updated_at=? WHERE id=?",
            (reason, now, task_id)
        )
        await db.commit()
    return await get_task(task_id)

async def reassign_task(task_id: int, assignee: str | None) -> dict | None:
    import json as _json
    now = datetime.now(dt_timezone.utc).isoformat()
    accept = _json.dumps([assignee] if assignee else [])
    async with _db_conn() as db:
        await db.execute(
            "UPDATE unified_tasks SET assigned_to=?, acceptable_assignees=?, updated_at=? WHERE id=?",
            (assignee, accept, now, task_id),
        )
        await db.commit()
    return await get_task(task_id)

async def get_task(task_id: int) -> dict | None:
    async with _db_conn() as db:
        cur = await db.execute("SELECT * FROM unified_tasks WHERE id=?", (task_id,))
        row = await cur.fetchone()
        return _row_to_task(row) if row else None

# c79.4 — admin/API list surfaces only (not BTS due scans like list_due_*).
# Default == hard max 100: clients cannot request unbounded "all" via high limit.
LIST_DEFAULT_LIMIT = 100
LIST_HARD_MAX = 100

def clamp_list_limit(limit: int | None = None, *, default: int = LIST_DEFAULT_LIMIT, hard_max: int = LIST_HARD_MAX) -> int:
    """Normalize list limit; raises ValueError if over hard_max (API maps to 400)."""
    if limit is None:
        return default
    try:
        n = int(limit)
    except (TypeError, ValueError) as e:
        raise ValueError(f"limit must be an integer, got {limit!r}") from e
    if n < 1:
        raise ValueError("limit must be >= 1")
    if n > hard_max:
        raise ValueError(f"limit {n} exceeds hard max {hard_max}")
    return n

def clamp_list_offset(offset: int | None = None) -> int:
    if offset is None:
        return 0
    try:
        n = int(offset)
    except (TypeError, ValueError) as e:
        raise ValueError(f"offset must be an integer, got {offset!r}") from e
    if n < 0:
        raise ValueError("offset must be >= 0")
    return n

async def list_tasks_for_person(
    person_id: str,
    status: str = "all",
    include_assigned_by: bool = True,
    *,
    limit: int | None = None,
    offset: int | None = None,
) -> list[dict]:
    from task_access import person_id_db_forms
    from task_status import legacy_status_to_unified
    bare, prefixed = person_id_db_forms(person_id)
    where = ["status != 'archived'"]
    params: list = []
    if include_assigned_by:
        where.append(
            "(LOWER(COALESCE(assigned_to, '')) IN (?, ?) "
            "OR LOWER(COALESCE(assigned_by, '')) IN (?, ?))"
        )
        params += [bare, prefixed, bare, prefixed]
    else:
        where.append("LOWER(COALESCE(assigned_to, '')) IN (?, ?)")
        params += [bare, prefixed]
    lanes = legacy_status_to_unified(status)
    if lanes:
        where.append(f"status IN ({','.join('?' * len(lanes))})"); params += list(lanes)
    if status == "approved":   # 'done' lane + approved_at distinguishes approved from awaiting-approval
        where.append("approved_at IS NOT NULL")
    lim = clamp_list_limit(limit)
    off = clamp_list_offset(offset)
    q = (
        f"SELECT * FROM unified_tasks WHERE {' AND '.join(where)} "
        f"ORDER BY created_at DESC LIMIT ? OFFSET ?"
    )
    params += [lim, off]
    async with _db_conn() as db:
        cur = await db.execute(q, tuple(params))
        return [_row_to_task(r) for r in await cur.fetchall()]

async def list_cognitive_tasks_as_system(limit: int = 20) -> list[dict]:
    """Project recent cognitive_tasks as read-only 'system' cards for the HUD."""
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        cur = await db.execute(
            """SELECT id, type, status, payload, created_at, completed_at, heartbeat, error,
                      started_at, model_used, tokens_in, tokens_out, duration_ms, gpu_ms, retry_count
               FROM cognitive_tasks
               WHERE status IN ('active', 'queued', 'done')
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        )
        return [_system_task_row_from_cognitive(dict(r), now=now) for r in await cur.fetchall()]

async def get_system_task_detail(virtual_id: int) -> dict | None:
    """Detail payload for a projected system card (negative virtual id)."""
    from database.cognitive import get_cognitive_task
    cog_id = cognitive_id_from_system_virtual(virtual_id)
    if cog_id is None:
        return None
    row = await get_cognitive_task(cog_id)
    if not row:
        return None
    task = _system_task_row_from_cognitive(row)
    runs: list[dict] = []
    if row.get("started_at"):
        run_status = "active" if row["status"] == "active" else (
            "completed" if row["status"] == "done" else row["status"]
        )
        runs.append({
            "execution_id": f"cognitive-{cog_id}",
            "status": run_status,
            "started_at": row["started_at"],
            "completed_at": row.get("completed_at"),
            "logs": row.get("error"),
            "metrics": {
                "model": row.get("model_used"),
                "tokens_in": row.get("tokens_in"),
                "tokens_out": row.get("tokens_out"),
                "duration_ms": row.get("duration_ms"),
                "gpu_ms": row.get("gpu_ms"),
                "retry_count": row.get("retry_count"),
            },
        })
    events: list[dict] = []
    if row.get("error"):
        events.append({
            "event_type": "error",
            "actor_person_id": None,
            "metadata": {"text": row["error"]},
            "created_at": row.get("completed_at") or row.get("started_at") or row.get("created_at"),
        })
    return {"task": task, "runs": runs, "events": events, "parents": [], "children": []}

async def list_all_tasks(
    status: str = "all",
    *,
    limit: int | None = None,
    offset: int | None = None,
) -> list[dict]:
    from task_status import legacy_status_to_unified
    where = ["status != 'archived'"]; params: list = []
    lanes = legacy_status_to_unified(status)
    if lanes:
        where.append(f"status IN ({','.join('?' * len(lanes))})"); params += list(lanes)
    if status == "approved":   # 'done' lane + approved_at distinguishes approved from awaiting-approval
        where.append("approved_at IS NOT NULL")
    lim = clamp_list_limit(limit)
    off = clamp_list_offset(offset)
    q = (
        f"SELECT * FROM unified_tasks WHERE {' AND '.join(where)} "
        f"ORDER BY created_at DESC LIMIT ? OFFSET ?"
    )
    params += [lim, off]
    async with _db_conn() as db:
        cur = await db.execute(q, tuple(params))
        return [_row_to_task(r) for r in await cur.fetchall()]

async def complete_task(task_id: int, completion_note: str = "") -> dict | None:
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            """UPDATE unified_tasks
               SET status='done', completed_at=?, completion_note=?, updated_at=?
               WHERE id=? AND status IN ('triage','todo','ready','running','blocked')""",
            (now, completion_note or "", now, task_id),
        )
        await db.commit()
    return await get_task(task_id)

async def snooze_task(task_id: int, snooze_until: str) -> dict | None:
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            """UPDATE unified_tasks
               SET snooze_until=?, snooze_count=COALESCE(snooze_count,0)+1, updated_at=?
               WHERE id=? AND status IN ('triage','todo','ready','running','blocked')""",
            (snooze_until, now, task_id),
        )
        await db.commit()
    return await get_task(task_id)

async def mark_task_prompted(task_id: int, prompted_at: str) -> None:
    async with _db_conn() as db:
        await db.execute(
            "UPDATE unified_tasks SET last_prompted_at=?, updated_at=? WHERE id=?",
            (prompted_at, prompted_at, task_id),
        )
        await db.commit()

async def mark_task_escalated(task_id: int, escalated_at: str) -> None:
    async with _db_conn() as db:
        await db.execute(
            "UPDATE unified_tasks SET escalated_at=?, updated_at=? WHERE id=?",
            (escalated_at, escalated_at, task_id),
        )
        await db.commit()

async def approve_task(task_id: int, approved: bool) -> dict | None:
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        if approved:
            # completed-and-awaiting-approval is unified status='done' with approved_at NULL
            await db.execute(
                "UPDATE unified_tasks SET approved_at=?, updated_at=? WHERE id=? AND status='done'",
                (now, now, task_id))
        else:
            # reject: send the task back to active 'todo', clear completion/approval
            await db.execute(
                """UPDATE unified_tasks
                   SET status='todo', approved_at=NULL, completed_at=NULL, updated_at=?
                   WHERE id=? AND status='done'""",
                (now, task_id))
        await db.commit()
    return await get_task(task_id)

async def delete_task(task_id: int) -> None:
    async with _db_conn() as db:
        await db.execute("DELETE FROM task_events WHERE task_id=?", (task_id,))
        await db.execute("DELETE FROM task_executions WHERE task_id=?", (task_id,))
        await db.execute("DELETE FROM task_links WHERE parent_id=? OR child_id=?", (task_id, task_id))
        await db.execute("DELETE FROM unified_tasks WHERE id=?", (task_id,))
        await db.commit()

async def list_due_tasks(now_iso: str) -> list[dict]:
    async with _db_conn() as db:
        cur = await db.execute(
            """SELECT * FROM unified_tasks
               WHERE status IN ('triage','todo','ready')
                 AND due_at IS NOT NULL AND due_at != '' AND due_at <= ?
                 AND (snooze_until IS NULL OR snooze_until <= ?)
               ORDER BY due_at ASC""",
            (now_iso, now_iso),
        )
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

async def _reachable(db, start: int, target: int) -> bool:
    seen, stack = set(), [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        cur = await db.execute("SELECT child_id FROM task_links WHERE parent_id=?", (node,))
        stack += [r[0] for r in await cur.fetchall()]
    return False

async def link_tasks(parent_id: int, child_id: int) -> bool:
    """parent->child dependency. Returns False (writes nothing) if it would cycle."""
    if parent_id == child_id:
        return False
    async with _db_conn() as db:
        if await _reachable(db, child_id, parent_id):
            return False
        await db.execute("INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                         (parent_id, child_id))
        await db.commit()
    return True

async def promote_ready_tasks() -> list[int]:
    """Promote 'todo' tasks whose linked parents are all 'done' to 'ready'. Returns promoted ids.

    40B-2C: N+1 eliminated — single UPDATE ... RETURNING with correlated NOT EXISTS subquery
    (no Python per-child loop or repeated SELECTs per candidate). Matches 40-SPEC.md §2C
    and 40-PLAN.md Slice 2C primary deliverable.
    """
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        cur = await db.execute(
            """UPDATE unified_tasks
               SET status='ready', updated_at=?
               WHERE status='todo'
                 AND id IN (
                   SELECT l.child_id FROM task_links l
                   WHERE NOT EXISTS (
                     SELECT 1 FROM task_links l2
                     JOIN unified_tasks p ON p.id = l2.parent_id
                     WHERE l2.child_id = l.child_id AND p.status != 'done'
                   )
                 )
               RETURNING id""",
            (now,),
        )
        promoted = [row[0] for row in await cur.fetchall()]
        await db.commit()
    return promoted

async def start_execution(task_id: int, execution_id: str) -> dict:
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            "INSERT OR IGNORE INTO task_executions (execution_id, task_id, status, started_at) VALUES (?, ?, 'active', ?)",
            (execution_id, task_id, now))
        await db.execute("UPDATE unified_tasks SET current_run_id=?, status='running', updated_at=? WHERE id=?",
                         (execution_id, now, task_id))
        await db.commit()
    return {"execution_id": execution_id, "task_id": task_id, "status": "active", "started_at": now}

async def finish_execution(execution_id: str, status: str, logs: str | None = None,
                           metrics: dict | None = None) -> None:
    import json as _json
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            "UPDATE task_executions SET status=?, completed_at=?, logs=?, metrics=? WHERE execution_id=?",
            (status, now, logs, _json.dumps(metrics or {}), execution_id))
        await db.commit()

async def list_executions(task_id: int) -> list[dict]:
    import json as _json
    async with _db_conn() as db:
        cur = await db.execute("SELECT * FROM task_executions WHERE task_id=? ORDER BY started_at DESC", (task_id,))
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            try: d["metrics"] = _json.loads(d.get("metrics") or "{}")
            except (ValueError, TypeError): d["metrics"] = {}
            out.append(d)
        return out

async def add_task_event(task_id: int, event_type: str, actor_person_id: str | None, metadata: dict | None = None) -> None:
    import json
    now = datetime.now(dt_timezone.utc).isoformat()
    data = json.dumps(metadata or {})
    async with _db_conn() as db:
        await db.execute(
            "INSERT INTO task_events (task_id, event_type, actor_person_id, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, event_type, actor_person_id, data, now),
        )
        await db.commit()

async def list_task_events(task_id: int) -> list[dict]:
    import json as _json
    async with _db_conn() as db:
        cur = await db.execute(
            "SELECT event_type, actor_person_id, metadata, created_at "
            "FROM task_events WHERE task_id=? ORDER BY created_at ASC",
            (task_id,)
        )
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            try:
                d["metadata"] = _json.loads(d.get("metadata") or "{}")
            except (ValueError, TypeError):
                d["metadata"] = {}
            out.append(d)
        return out

async def get_task_links(task_id: int) -> dict:
    """Return parent and child task ids for task_id.

    Returns {"parents": [int, ...], "children": [int, ...]}
    """
    async with _db_conn() as conn:
        cur = await conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id=?", (task_id,)
        )
        children = [row[0] for row in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id=?", (task_id,)
        )
        parents = [row[0] for row in await cur.fetchall()]
    return {"parents": parents, "children": children}

def _row_to_automation(row: sqlite3.Row) -> dict:
    import json
    payload = {}
    try:
        payload = json.loads(row["schedule_payload"] or "{}")
    except Exception:
        payload = {}
    return {
        "id": row["id"],
        "title": row["title"],
        "message": row["message"],
        "person_id": row["person_id"],
        "audience_scope": row["audience_scope"] or "self",
        "schedule_kind": row["schedule_kind"] or "weekly",
        "schedule_payload": payload,
        "timezone": row["timezone"],
        "next_run_at": row["next_run_at"],
        "is_active": bool(row["is_active"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_triggered_at": row["last_triggered_at"],
    }

async def create_automation(
    title: str,
    message: str,
    person_id: str,
    schedule_kind: str,
    schedule_payload: dict,
    timezone: str,
    created_by: str,
    audience_scope: str = "self",
    next_run_at: str | None = None,
) -> dict:
    import json
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO automations
               (title, message, person_id, audience_scope, schedule_kind, schedule_payload,
                timezone, next_run_at, is_active, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (
                title,
                message,
                person_id,
                audience_scope,
                schedule_kind,
                json.dumps(schedule_payload or {}),
                timezone,
                next_run_at,
                created_by,
                now,
                now,
            ),
        )
        cur = await db.execute("SELECT last_insert_rowid()")
        row = await cur.fetchone()
        aid = int(row[0])
        await db.commit()
    return await get_automation(aid)

async def get_automation(automation_id: int) -> dict | None:
    async with _db_conn() as db:

        cur = await db.execute("SELECT * FROM automations WHERE id=?", (automation_id,))
        row = await cur.fetchone()
        return _row_to_automation(row) if row else None

async def list_automations_for_person(
    person_id: str,
    include_created_by: bool = True,
    *,
    limit: int | None = None,
    offset: int | None = None,
) -> list[dict]:
    where = "person_id = ?"
    params: list = [person_id]
    if include_created_by:
        where = "(person_id = ? OR created_by = ?)"
        params = [person_id, person_id]
    lim = clamp_list_limit(limit)
    off = clamp_list_offset(offset)
    params += [lim, off]
    async with _db_conn() as db:
        cur = await db.execute(
            f"SELECT * FROM automations WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(params),
        )
        rows = await cur.fetchall()
        return [_row_to_automation(r) for r in rows]

async def list_active_automations() -> list[dict]:
    async with _db_conn() as db:

        cur = await db.execute("SELECT * FROM automations WHERE is_active=1 ORDER BY created_at DESC")
        rows = await cur.fetchall()
        return [_row_to_automation(r) for r in rows]

async def list_all_automations(
    *,
    limit: int | None = None,
    offset: int | None = None,
) -> list[dict]:
    lim = clamp_list_limit(limit)
    off = clamp_list_offset(offset)
    async with _db_conn() as db:
        cur = await db.execute(
            "SELECT * FROM automations ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (lim, off),
        )
        rows = await cur.fetchall()
        return [_row_to_automation(r) for r in rows]

async def set_automation_active(automation_id: int, is_active: bool) -> dict | None:
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            "UPDATE automations SET is_active=?, updated_at=? WHERE id=?",
            (int(is_active), now, automation_id),
        )
        await db.commit()
    return await get_automation(automation_id)

async def mark_automation_triggered(automation_id: int, triggered_at: str, next_run_at: str | None) -> None:
    async with _db_conn() as db:
        await db.execute(
            "UPDATE automations SET last_triggered_at=?, next_run_at=?, updated_at=? WHERE id=?",
            (triggered_at, next_run_at, triggered_at, automation_id),
        )
        await db.commit()

async def set_automation_next_run(automation_id: int, next_run_at: str | None) -> None:
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            "UPDATE automations SET next_run_at=?, updated_at=? WHERE id=?",
            (next_run_at, now, automation_id),
        )
        await db.commit()

async def list_due_automations(now_iso: str) -> list[dict]:
    async with _db_conn() as db:

        cur = await db.execute(
            """SELECT * FROM automations
               WHERE is_active=1
                 AND next_run_at IS NOT NULL
                 AND next_run_at <= ?
               ORDER BY next_run_at ASC""",
            (now_iso,),
        )
        rows = await cur.fetchall()
        return [_row_to_automation(r) for r in rows]

async def delete_automation(automation_id: int) -> None:
    async with _db_conn() as db:
        await db.execute("DELETE FROM automations WHERE id=?", (automation_id,))
        await db.commit()

async def get_stale_unified_runs(older_than_minutes: int = UNIFIED_RECLAIM_TIMEOUT_MINUTES) -> list[dict]:
    """Return running unified tasks whose heartbeat is stale (zombie agent runs)."""
    cutoff = (datetime.now(dt_timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
    async with _db_conn() as db:
        async with db.execute(
            """SELECT * FROM unified_tasks
               WHERE status='running'
                 AND type != 'system'
                 AND (
                   (heartbeat IS NOT NULL AND heartbeat < ?)
                   OR (heartbeat IS NULL AND updated_at < ?)
                 )""",
            (cutoff, cutoff),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def reclaim_stalled_unified_tasks(older_than_minutes: int = UNIFIED_RECLAIM_TIMEOUT_MINUTES) -> list[int]:
    """Reclaim stale running unified tasks back to 'ready'. Returns list of reclaimed ids.
    Appends a 'reclaimed' task_event for each. Safe no-op if nothing is stale.
    """
    now = datetime.now(dt_timezone.utc).isoformat()
    stale = await get_stale_unified_runs(older_than_minutes)
    reclaimed: list[int] = []
    async with _db_conn() as db:
        for row in stale:
            tid = row["id"]
            await db.execute(
                "UPDATE unified_tasks SET status='ready', current_run_id=NULL, heartbeat=NULL, updated_at=? WHERE id=?",
                (now, tid),
            )
            reclaimed.append(tid)
        await db.commit()
    for row in stale:
        await add_task_event(
            row["id"], "reclaimed", None,
            {"reason": "heartbeat timeout (zombie)", "old_run_id": row.get("current_run_id")},
        )
    return reclaimed

async def set_unified_task_running(task_id: int) -> None:
    from datetime import datetime, timezone
    async with db_conn() as c:
        await c.execute(
            "UPDATE unified_tasks SET status='running', updated_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), task_id),
        )
        await c.commit()

