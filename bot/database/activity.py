"""database.activity — domain module (8lx.1 Phase 1)."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import json as _json
import pathlib as _pathlib
import sqlite_async

from database.conn import (
    HFX,
    _db_conn,
    _db_read,
    _get_connection,
    _get_init_lock,
    _get_lock,
    _pkg,
    _resolve_db_path,
    close_db,
    db_conn,
    wal_checkpoint_passive,
)

log = logging.getLogger("database.activity")

async def log_activity(event_type: str, description: str, meta: str = None, channel: str = None, person_id: str = None):
    import json
    metadata_dict = {}
    if meta:
        metadata_dict["meta"] = meta
    if channel:
        metadata_dict["chan"] = channel
    metadata_json = json.dumps(metadata_dict) if metadata_dict else None

    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO activity_log (event_type, description, person_id, metadata)
               VALUES (?, ?, ?, ?)""",
            (event_type, description, person_id, metadata_json)
        )
        await db.commit()

async def log_turn_timing(
    *,
    turn_id: str,
    total_ms: int,
    setup_ms: int = 0,
    context_ms: int = 0,
    llm_ms: int = 0,
    tools_ms: int = 0,
    send_ms: int = 0,
    channel_id: str | None = None,
    person_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Log E2E turn breakdown to activity_log as 'turn_timing'.

    Safe, fire-and-forget friendly. No schema migration required.
    """
    meta = {
        "turn_id": turn_id,
        "total_ms": int(total_ms or 0),
        "setup_ms": int(setup_ms or 0),
        "context_ms": int(context_ms or 0),
        "llm_ms": int(llm_ms or 0),
        "tools_ms": int(tools_ms or 0),
        "send_ms": int(send_ms or 0),
    }
    if metadata:
        meta.update(metadata)
    await log_activity(
        "turn_timing",
        f"turn {turn_id} total={total_ms}ms",
        meta=_json.dumps(meta),
        channel=channel_id,
        person_id=person_id,
    )

async def log_context_build(
    *,
    turn_id: str | None = None,
    presence_ms: int = 0,
    ha_ms: int = 0,
    calendar_ms: int = 0,
    weather_ms: int = 0,
    total_ms: int = 0,
    calendar_cache_hit: bool | None = None,
    channel_id: str | None = None,
    person_id: str | None = None,
) -> None:
    """Log per-leg context timing (build_context legs) as 'context_build'."""
    meta: dict = {
        "turn_id": turn_id,
        "presence_ms": int(presence_ms or 0),
        "ha_ms": int(ha_ms or 0),
        "calendar_ms": int(calendar_ms or 0),
        "weather_ms": int(weather_ms or 0),
        "total_ms": int(total_ms or 0),
    }
    if calendar_cache_hit is not None:
        meta["calendar_cache_hit"] = bool(calendar_cache_hit)
    await log_activity(
        "context_build",
        f"context {turn_id or 'n/a'} total={total_ms}ms",
        meta=_json.dumps(meta),
        channel=channel_id,
        person_id=person_id,
    )

async def log_tool_surface(
    *,
    turn_id: str | None = None,
    tool_count: int = 0,
    domains: list[str] | None = None,
    narrowed: bool = False,
    channel_id: str | None = None,
    person_id: str | None = None,
) -> None:
    """Log intent-router tool surface for perf soak analysis."""
    meta: dict = {
        "turn_id": turn_id,
        "tool_count": int(tool_count),
        "narrowed": bool(narrowed),
        "domains": domains if domains is not None else None,
    }
    await log_activity(
        "tool_surface",
        f"tools={tool_count} narrowed={narrowed}",
        meta=_json.dumps(meta),
        channel=channel_id,
        person_id=person_id,
    )

async def log_llm_iteration(
    *,
    turn_id: str | None = None,
    step: int,
    prompt_hash: str | None = None,
    tokens_in: int = 0,
    delta_tokens: int = 0,
    model: str | None = None,
    latency_ms: int | None = None,
    stop_reason: str | None = None,
    channel_id: str | None = None,
    person_id: str | None = None,
) -> None:
    """Log per-iteration LLM step (prompt_hash + delta) as 'llm_iteration'."""
    meta = {
        "turn_id": turn_id,
        "step": int(step),
        "prompt_hash": prompt_hash,
        "tokens_in": int(tokens_in or 0),
        "delta_tokens": int(delta_tokens or 0),
        "model": model,
        "latency_ms": latency_ms,
        "stop_reason": stop_reason,
    }
    await log_activity(
        "llm_iteration",
        f"llm step {step} hash={prompt_hash} delta={delta_tokens}",
        meta=_json.dumps(meta),
        channel=channel_id,
        person_id=person_id,
    )

async def get_activity_log(limit: int = 50):
    async with _db_read() as db:
        cur = await db.execute(
            """SELECT event_type, description, person_id, metadata, logged_at
               FROM activity_log ORDER BY logged_at DESC LIMIT ?""",
            (limit,)
        )
        rows = await cur.fetchall()
        import json
        result = []
        for r in rows:
            meta = json.loads(r[3]) if r[3] else {}
            result.append({
                "kind": r[0],
                "body": r[1],
                "time": r[4], # Will format in API
                "meta": meta.get("meta", ""),
                "chan": meta.get("chan", ""),
                "who": r[2]
            })
        return result

async def search_conversation_history(
    query: str,
    *,
    limit: int = 20,
    channel_id: int | None = None,
    since_days: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """5hy.11 session_search discover: FTS5 over conversation_history."""
    if not query or not str(query).strip():
        return []
    lim = max(1, min(int(limit or 20), 50))
    off = max(0, int(offset or 0))
    async with _db_read() as db:
        params: list = [query.strip()]
        sql = """
            SELECT
                c.id,
                c.channel_id,
                c.role,
                c.content,
                c.created_at,
                bm25(conversation_history_fts) AS rank
            FROM conversation_history_fts
            JOIN conversation_history c ON c.id = conversation_history_fts.rowid
            WHERE conversation_history_fts MATCH ?
        """
        if channel_id is not None:
            sql += " AND c.channel_id = ?"
            params.append(int(channel_id))
        if since_days is not None:
            cutoff = (datetime.now(dt_timezone.utc) - timedelta(days=int(since_days))).isoformat()
            sql += " AND c.created_at >= ?"
            params.append(cutoff)
        sql += " ORDER BY rank LIMIT ? OFFSET ?"
        params.extend([lim, off])
        cur = await db.execute(sql, tuple(params))
        rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "channel_id": r[1],
                "role": r[2],
                "content": r[3],
                "created_at": r[4],
                "snippet": (r[3] or "")[:300],
                "rank": round(r[5], 4) if r[5] is not None else None,
            }
            for r in rows
        ]


async def browse_conversation_history(
    *,
    around_id: int | None = None,
    channel_id: int | None = None,
    limit: int = 20,
) -> list[dict]:
    """5hy.11 browse: messages around an id or latest for a channel."""
    lim = max(1, min(int(limit or 20), 50))
    async with _db_read() as db:
        if around_id is not None:
            # window of lim/2 before and after
            half = max(1, lim // 2)
            cur = await db.execute(
                """SELECT id, channel_id, role, content, created_at
                   FROM conversation_history
                   WHERE id <= ?
                   ORDER BY id DESC LIMIT ?""",
                (int(around_id), half + 1),
            )
            before = list(reversed(await cur.fetchall()))
            cur = await db.execute(
                """SELECT id, channel_id, role, content, created_at
                   FROM conversation_history
                   WHERE id > ?
                   ORDER BY id ASC LIMIT ?""",
                (int(around_id), half),
            )
            after = await cur.fetchall()
            rows = before + after
        else:
            where = "1=1"
            params: list = []
            if channel_id is not None:
                where = "channel_id = ?"
                params.append(int(channel_id))
            params.append(lim)
            cur = await db.execute(
                f"""SELECT id, channel_id, role, content, created_at
                    FROM conversation_history
                    WHERE {where}
                    ORDER BY id DESC LIMIT ?""",
                tuple(params),
            )
            rows = list(reversed(await cur.fetchall()))
        return [
            {
                "id": r[0],
                "channel_id": r[1],
                "role": r[2],
                "content": r[3],
                "created_at": r[4],
                "snippet": (r[3] or "")[:300],
            }
            for r in rows
        ]


async def scroll_conversation_history(
    *,
    channel_id: int | None = None,
    before_id: int | None = None,
    after_id: int | None = None,
    limit: int = 20,
) -> list[dict]:
    """5hy.11 scroll: page conversation_history by id cursor."""
    lim = max(1, min(int(limit or 20), 50))
    clauses = []
    params: list = []
    if channel_id is not None:
        clauses.append("channel_id = ?")
        params.append(int(channel_id))
    if before_id is not None:
        clauses.append("id < ?")
        params.append(int(before_id))
    if after_id is not None:
        clauses.append("id > ?")
        params.append(int(after_id))
    where = " AND ".join(clauses) if clauses else "1=1"
    order = "ASC" if after_id is not None and before_id is None else "DESC"
    params.append(lim)
    async with _db_read() as db:
        cur = await db.execute(
            f"""SELECT id, channel_id, role, content, created_at
                FROM conversation_history
                WHERE {where}
                ORDER BY id {order} LIMIT ?""",
            tuple(params),
        )
        rows = await cur.fetchall()
        if order == "DESC":
            rows = list(reversed(rows))
        return [
            {
                "id": r[0],
                "channel_id": r[1],
                "role": r[2],
                "content": r[3],
                "created_at": r[4],
                "snippet": (r[3] or "")[:300],
            }
            for r in rows
        ]


async def search_activity_log(
    query: str,
    limit: int = 20,
    since_days: int | None = None,
    person_id: str | None = None,
    event_type: str | None = None,
) -> list[dict]:
    """FTS5-powered search over activity_log with bm25 ranking.

    Note: bm25() returns lower scores for better matches (standard FTS5 behavior).
    Returns the most relevant entries with useful context for the caller.
    """
    import json
    from datetime import datetime, timedelta, timezone

    if not query or not query.strip():
        return []

    async with _db_read() as db:
        params = []
        where_clauses = []

        sql = """
            SELECT
                a.id,
                a.logged_at,
                a.event_type,
                a.description,
                a.person_id,
                a.metadata,
                bm25(activity_log_fts) AS rank
            FROM activity_log_fts
            JOIN activity_log a ON a.id = activity_log_fts.rowid
            WHERE activity_log_fts MATCH ?
        """
        params.append(query)

        if since_days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
            where_clauses.append("a.logged_at >= ?")
            params.append(cutoff)

        if person_id:
            where_clauses.append("a.person_id = ?")
            params.append(person_id)

        if event_type:
            where_clauses.append("a.event_type = ?")
            params.append(event_type)

        if where_clauses:
            sql += " AND " + " AND ".join(where_clauses)

        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        cur = await db.execute(sql, params)
        rows = await cur.fetchall()

        results = []
        for r in rows:
            meta = json.loads(r[5]) if r[5] else {}
            results.append({
                "id": r[0],
                "time": r[1],
                "kind": r[2],
                "description": r[3],
                "person_id": r[4],
                "channel": meta.get("chan") or meta.get("channel"),
                "actor": meta.get("who") or meta.get("actor"),
                "snippet": (r[3] or "")[:300] if r[3] else "",
                "rank": round(r[6], 4) if r[6] is not None else None,
            })
        return results

async def conversation_history_in_range(utc_start: str, utc_end: str) -> list[dict]:
    async with _db_read() as db:
        db.row_factory = sqlite3.Row
        cur = await db.execute(
            """SELECT channel_id, role, content, created_at FROM conversation_history
               WHERE created_at >= ? AND created_at < ? ORDER BY created_at ASC""",
            (utc_start, utc_end),
        )
        rows = await cur.fetchall()
        return [
            {"channel_id": str(r["channel_id"]), "role": r["role"], "content": r["content"], "created_at": r["created_at"]}
            for r in rows
        ]

async def fetch_conversation_rows_since(since_iso: str) -> list[dict]:
    """Return conversation rows with ``created_at >= since_iso``.

    Storage contract: ``add_message`` writes ``datetime.now(UTC).isoformat()``
    (``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``). Callers must pass the same ISO shape
    (e.g. ``datetime.now(timezone.utc).isoformat()``). Do **not** pass
    activity_log space timestamps here — see ``normalize_since_for_activity_log``.
    """
    bound = (since_iso or "").strip()
    async with _db_read() as db:
        db.row_factory = sqlite3.Row
        async with db.execute(
            """SELECT id, channel_id, role, content, created_at FROM conversation_history
               WHERE created_at >= ? ORDER BY id ASC""",
            (bound,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

def normalize_since_for_activity_log(since_iso: str) -> str:
    """Normalize a since-bound for ``activity_log.logged_at`` lexicographic ``>=``.

    ``activity_log.logged_at`` uses SQLite ``CURRENT_TIMESTAMP`` style
    ``YYYY-MM-DD HH:MM:SS`` (space, no tz). Incoming ISO bounds (from
    ``datetime.isoformat()``) must be converted or rows are silently skipped
    (space sorts before ``T`` at the same calendar date).

    Truncates to whole seconds — slightly wider window when micros are present.
    See CLAUDE.md "Date / Time Standards" (param bounds, not ``LIKE``).
    """
    s = (since_iso or "").strip()
    if not s:
        return s

    # Already in activity_log storage shape.
    if "T" not in s and " " in s and not s.endswith("Z") and "+" not in s:
        return s.split(".", 1)[0].strip()

    try:
        normalized = s.replace("Z", "+00:00")
        if " " in normalized and "T" not in normalized:
            normalized = normalized.replace(" ", "T", 1)
        dt = datetime.fromisoformat(normalized)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass

    # Fallback for odd legacy strings (keep prior munging behaviour).
    s = s.replace("T", " ", 1)
    if "+" in s:
        s = s.split("+", 1)[0]
    elif s.endswith("Z"):
        s = s[:-1]
    if "." in s:
        s = s.split(".", 1)[0]
    return s.strip()

async def fetch_tool_calls_since(since_iso: str) -> list[dict]:
    """Return tool_call activity rows with ``logged_at >=`` normalized bound."""
    since_norm = normalize_since_for_activity_log(since_iso)
    async with _db_read() as db:
        db.row_factory = sqlite3.Row
        async with db.execute(
            """SELECT logged_at, description FROM activity_log
               WHERE event_type='tool_call' AND logged_at >= ?
               ORDER BY id ASC""",
            (since_norm,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def fetch_activity_since(event_type: str, since_iso: str) -> list[dict]:
    """Return activity_log rows of a given event_type with logged_at >= bound."""
    since_norm = normalize_since_for_activity_log(since_iso)
    async with _db_read() as db:
        db.row_factory = sqlite3.Row
        async with db.execute(
            """SELECT logged_at, description, person_id, metadata FROM activity_log
               WHERE event_type=? AND logged_at >= ?
               ORDER BY id DESC""",
            (event_type, since_norm),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def conversation_snippet_for_title(
    channel_id: int, start_dt: str, end_dt: str, *, limit: int = 3
) -> list[dict]:
    async with _db_read() as db:
        db.row_factory = sqlite3.Row
        cur = await db.execute(
            """SELECT role, content FROM conversation_history
               WHERE channel_id = ? AND created_at >= ? AND created_at <= ?
               ORDER BY created_at ASC LIMIT ?""",
            (channel_id, start_dt, end_dt, limit),
        )
        rows = await cur.fetchall()
        if rows:
            return [{"role": r["role"], "content": r["content"]} for r in rows]
        cur = await db.execute(
            """SELECT user_message AS content FROM shadow_calls
               WHERE channel_id = ? AND created_at >= ? AND created_at <= ?
               ORDER BY created_at ASC LIMIT ?""",
            (str(channel_id), start_dt, end_dt, limit),
        )
        rows = await cur.fetchall()
        return [{"role": "user", "content": r["content"]} for r in rows if r["content"]]

