"""database.cognitive — domain module (8lx.1 Phase 1)."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import json as _json
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

log = logging.getLogger("database.cognitive")

# Research memory thread log key (Phase 29); kept with research_memory APIs.
_RESEARCH_THREAD_LOG_KEY = "thread:log"

async def create_cognitive_task(
    type: str,
    payload: dict,
    priority: int = 0,
    run_at: str | None = None,
    max_retries: int = 3,
    actor_id: str | None = None,
    channel_id: str | None = None,
) -> int:
    """Enqueue a cognitive task. Returns the new task id."""
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        cur = await db.execute(
            """INSERT INTO cognitive_tasks
               (type, status, payload, priority, run_at, created_at, max_retries, actor_id, channel_id)
               VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?)""",
            (type, _json.dumps(payload, default=str), priority, run_at or now, now,
             max_retries, actor_id, channel_id),
        )
        await db.commit()
        return cur.lastrowid

async def claim_next_task() -> dict | None:
    """Atomically claim the next queued task whose run_at <= now. Returns the task or None.

    Uses BEGIN IMMEDIATE (per 40-SPEC Appendix A) for the short critical section
    inside the write lock. The conditional UPDATE + rowcount handles cross-process
    races (even though 40A makes cognition the sole writer).
    """
    import json
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            """SELECT id FROM cognitive_tasks
               WHERE status='queued' AND run_at <= ?
               ORDER BY priority DESC, created_at ASC LIMIT 1""",
            (now,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await db.execute("ROLLBACK")
            return None
        task_id = row["id"]
        # The conditional UPDATE is the claim: only the writer whose UPDATE
        # actually flips queued→active (rowcount == 1) owns the task.
        # Rowcount check protects against races (cross-process or otherwise).
        upd = await db.execute(
            """UPDATE cognitive_tasks
               SET status='active', started_at=?, heartbeat=?
               WHERE id=? AND status='queued'""",
            (now, now, task_id),
        )
        await db.commit()
        if upd.rowcount != 1:
            return None  # lost the race to another worker
        async with db.execute("SELECT * FROM cognitive_tasks WHERE id=?", (task_id,)) as cur:
            claimed = await cur.fetchone()
        if claimed and claimed["status"] == "active":
            d = dict(claimed)
            try:
                d["payload"] = json.loads(d["payload"] or "{}")
            except Exception:
                pass
            return d
    return None

async def update_task_heartbeat(task_id: int) -> None:
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            "UPDATE cognitive_tasks SET heartbeat=? WHERE id=?", (now, task_id)
        )
        await db.commit()

async def complete_cognitive_task(task_id: int, result: dict | None = None) -> None:
    import json
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            """UPDATE cognitive_tasks
               SET status='done', completed_at=?, result=?
               WHERE id=?""",
            (now, json.dumps(result) if result else None, task_id),
        )
        await db.commit()

async def get_cognitive_task(task_id: int) -> dict | None:
    """Fetch a single cognitive_task by id with payload decoded."""
    import json as _json
    async with _db_conn() as db:
        async with db.execute("SELECT * FROM cognitive_tasks WHERE id=?", (task_id,)) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = _json.loads(d.get("payload") or "{}")
    except Exception:
        d["payload"] = {}
    return d

async def update_cognitive_task_payload(task_id: int, updates: dict) -> bool:
    """Merge `updates` into the JSON payload of a cognitive_task. Returns True if updated."""
    import json as _json
    async with _db_conn() as db:
        async with db.execute(
            "SELECT payload FROM cognitive_tasks WHERE id=?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        try:
            current = _json.loads(row["payload"] or "{}")
        except Exception:
            current = {}
        current.update(updates)
        await db.execute(
            "UPDATE cognitive_tasks SET payload=? WHERE id=?",
            (_json.dumps(current), task_id),
        )
        await db.commit()
        return True

async def complete_cognitive_task_with_stats(
    task_id: int,
    result: dict | None = None,
    stats: dict | None = None,
) -> None:
    """Mark cognitive_task done and persist per-run cost columns.

    stats dict keys (all optional): model, tokens_in, tokens_out, duration_ms, gpu_ms.
    """
    import json as _json
    stats = stats or {}
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            """UPDATE cognitive_tasks
               SET status='done', completed_at=?, result=?,
                   model_used=?, tokens_in=?, tokens_out=?,
                   duration_ms=?, gpu_ms=?
               WHERE id=?""",
            (
                now,
                _json.dumps(result) if result else None,
                stats.get("model"),
                stats.get("tokens_in"),
                stats.get("tokens_out"),
                stats.get("duration_ms"),
                stats.get("gpu_ms"),
                task_id,
            ),
        )
        await db.commit()

async def fail_cognitive_task(task_id: int, error: str) -> None:
    """Increment retry_count; move to dead_letter if retries exhausted."""
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:

        async with db.execute(
            "SELECT retry_count, max_retries FROM cognitive_tasks WHERE id=?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return
        new_count = (row["retry_count"] or 0) + 1
        if new_count >= (row["max_retries"] or 3):
            new_status = "dead_letter"
        else:
            new_status = "queued"
            # Exponential back-off: retry after 2^n minutes
            delay = timedelta(minutes=2 ** new_count)
            run_at = (datetime.now(dt_timezone.utc) + delay).isoformat()
        await db.execute(
            """UPDATE cognitive_tasks
               SET status=?, retry_count=?, error=?, heartbeat=NULL,
                   run_at=COALESCE(?, run_at)
               WHERE id=?""",
            (new_status, new_count, error,
             run_at if new_status == "queued" else None, task_id),
        )
        await db.commit()

async def get_stale_active_tasks(older_than_minutes: int = 5) -> list[dict]:
    """Return active tasks whose heartbeat is stale (zombie detection)."""
    cutoff = (
        datetime.now(dt_timezone.utc) - timedelta(minutes=older_than_minutes)
    ).isoformat()
    async with _db_conn() as db:

        async with db.execute(
            """SELECT * FROM cognitive_tasks
               WHERE status='active' AND (heartbeat IS NULL OR heartbeat < ?)""",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def store_task_output(task_id: int, key: str, content: str) -> None:
    """Upsert task_outputs keyed by (task_id, key) — overwrites on conflict."""
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO task_outputs (task_id, key, content)
               VALUES (?, ?, ?)
               ON CONFLICT(task_id, key) DO UPDATE SET
                 content=excluded.content,
                 created_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
            (task_id, key, content),
        )
        await db.commit()

async def get_task_output(task_id: int, key: str) -> dict | None:
    """Fetch a single task_outputs row by (task_id, key)."""
    async with _db_conn() as db:
        async with db.execute(
            "SELECT * FROM task_outputs WHERE task_id=? AND key=?",
            (task_id, key),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def append_research_memory(unified_task_id: int, kind: str, content: str) -> None:
    """Append a research memory entry on a unified research task (Phase 29 Wave G)."""
    import json as _json
    from datetime import datetime, timezone

    row = await get_task_output(unified_task_id, _RESEARCH_THREAD_LOG_KEY)
    entries: list[dict] = []
    if row:
        try:
            entries = _json.loads(row.get("content") or "[]")
            if not isinstance(entries, list):
                entries = []
        except (_json.JSONDecodeError, TypeError):
            entries = []
    entries.append(
        {
            "kind": kind,
            "content": (content or "")[:4000],
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    await store_task_output(unified_task_id, _RESEARCH_THREAD_LOG_KEY, _json.dumps(entries))

async def find_research_tasks_by_title(title_substring: str, *, limit: int = 3) -> list[dict]:
    """Search unified research tasks by title substring."""
    q = f"%{(title_substring or '').strip().lower()}%"
    async with _db_conn() as db:
        async with db.execute(
            """SELECT id, title, status FROM unified_tasks
               WHERE type='research' AND LOWER(title) LIKE ?
               ORDER BY id DESC LIMIT ?""",
            (q, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def list_research_memory(unified_task_id: int) -> list[dict]:
    """List research thread memory entries for a unified task."""
    import json as _json

    row = await get_task_output(unified_task_id, _RESEARCH_THREAD_LOG_KEY)
    if not row:
        return []
    try:
        data = _json.loads(row.get("content") or "[]")
        return data if isinstance(data, list) else []
    except (_json.JSONDecodeError, TypeError):
        return []

def format_research_memory_for_prompt(entries: list[dict], *, max_chars: int = 3000) -> str:
    """Render prior research memory for worker prompt injection."""
    if not entries:
        return ""
    lines = ["Prior research on this task:"]
    for e in entries[-10:]:
        kind = e.get("kind", "note")
        content = (e.get("content") or "").strip()
        if content:
            lines.append(f"- [{kind}] {content[:500]}")
    text = "\n".join(lines)
    return text[:max_chars]

async def get_task_output_by_key(key: str) -> dict | None:
    """Most recent task_output for a given key, or None."""
    async with _db_conn() as db:

        async with db.execute(
            "SELECT * FROM task_outputs WHERE key=? ORDER BY created_at DESC LIMIT 1",
            (key,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def find_cognitive_task_by_payload_key(
    task_type: str, key: str, value: str
) -> dict | None:
    """Look up a cognitive_task whose payload JSON contains key=value.

    Used for idempotent enqueue — caller wants to know if a queued/active/done
    task already exists before creating a new one.
    """
    async with _db_conn() as db:

        # status filter excludes failures so a failed task does not block a retry
        async with db.execute(
            """SELECT * FROM cognitive_tasks
               WHERE type=? AND status NOT IN ('failed', 'dead_letter')
                 AND payload LIKE ?
               ORDER BY id DESC LIMIT 1""",
            (task_type, f'%"{key}": "{value}"%'),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def has_recent_cognitive_dead_letter(
    task_type: str,
    key: str,
    value: str,
    *,
    within_hours: int = 24,
) -> bool:
    """True if a dead_letter task for this payload key exists within the cooldown window."""
    cutoff = (datetime.now(dt_timezone.utc) - timedelta(hours=within_hours)).isoformat()
    pattern = f'%"{key}": "{value}"%'
    async with _db_conn() as db:
        async with db.execute(
            """SELECT 1 FROM cognitive_tasks
               WHERE type=? AND status='dead_letter'
                 AND payload LIKE ?
                 AND created_at >= ?
               LIMIT 1""",
            (task_type, pattern, cutoff),
        ) as cur:
            return (await cur.fetchone()) is not None

async def get_recent_cognitive_failures(hours: int = 24, limit: int = 30) -> list[dict]:
    """Return recent cognitive task failures (failed or dead_letter) for Watchman nightly audit.

    Higher-signal summary of repeated worker/Ollama issues than raw bot.log lines.
    Only public DB functions used from callers (e.g. watchman). Excludes active/queued/done
    (see list_cognitive_tasks_as_system for HUD view).
    """
    cutoff = (datetime.now(dt_timezone.utc) - timedelta(hours=hours)).isoformat()
    async with _db_conn() as db:
        async with db.execute(
            """SELECT id, type, status, error, created_at, model_used, retry_count
               FROM cognitive_tasks
               WHERE status IN ('failed', 'dead_letter') AND created_at >= ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (cutoff, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def get_cognitive_runs(limit: int = 50, offset: int = 0, task_type: str | None = None) -> list[dict]:
    async with _db_conn() as db:

        if task_type:
            q = "SELECT * FROM cognitive_tasks WHERE type=? ORDER BY id DESC LIMIT ? OFFSET ?"
            args = (task_type, limit, offset)
        else:
            q = "SELECT * FROM cognitive_tasks ORDER BY id DESC LIMIT ? OFFSET ?"
            args = (limit, offset)
        async with db.execute(q, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def get_cognitive_stats(days: int = 7) -> list[dict]:
    """Per-worker aggregates for the rolling window, including calculated costs."""
    from database.usage import _token_cost
    async with _db_conn() as db:
        async with db.execute(
            """SELECT type, status, duration_ms, model_used, tokens_in, tokens_out
               FROM cognitive_tasks
               WHERE created_at > datetime('now', ?)""",
            (f"-{days} days",),
        ) as cur:
            rows = await cur.fetchall()

    from collections import defaultdict
    grouped = defaultdict(lambda: {
        "runs": 0, "done": 0, "failed": 0, "durations": [], 
        "tokens_in": 0, "tokens_out": 0, "cost": 0.0
    })

    for r in rows:
        w_type = r[0]
        status = r[1]
        duration = r[2]
        model = r[3]
        tok_in = r[4] or 0
        tok_out = r[5] or 0

        g = grouped[w_type]
        g["runs"] += 1
        if status == "done":
            g["done"] += 1
        elif status in ("failed", "dead_letter"):
            g["failed"] += 1
        
        if duration is not None:
            g["durations"].append(duration)
            
        g["tokens_in"] += tok_in
        g["tokens_out"] += tok_out
        
        if tok_in or tok_out:
            g["cost"] += _token_cost(tok_in, tok_out, model)

    result = []
    for w_type, g in grouped.items():
        avg_dur = sum(g["durations"]) / len(g["durations"]) if g["durations"] else 0.0
        result.append({
            "type": w_type,
            "runs": g["runs"],
            "done": g["done"],
            "failed": g["failed"],
            "avg_duration_ms": avg_dur,
            "total_tokens_in": g["tokens_in"],
            "total_tokens_out": g["tokens_out"],
            "total_cost_usd": round(g["cost"], 5)
        })
        
    result.sort(key=lambda x: x["runs"], reverse=True)
    return result

async def get_cognitive_outputs(limit: int = 50, offset: int = 0) -> list[dict]:
    async with _db_conn() as db:

        async with db.execute(
            "SELECT id, task_id, key, length(content) AS content_len, created_at "
            "FROM task_outputs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def get_dead_letter_tasks_since(hours: int = 24) -> list[dict]:
    """Return cognitive_tasks that entered dead_letter in the last `hours` hours."""
    async with _db_conn() as db:
        async with db.execute(
            """SELECT id, type, status, retry_count, error,
                      COALESCE(tokens_in, 0) AS tokens_in,
                      COALESCE(tokens_out, 0) AS tokens_out,
                      created_at, completed_at
               FROM cognitive_tasks
               WHERE status = 'dead_letter'
                 AND COALESCE(completed_at, created_at) > datetime('now', ?)
               ORDER BY COALESCE(completed_at, created_at) DESC""",
            (f"-{hours} hours",),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

