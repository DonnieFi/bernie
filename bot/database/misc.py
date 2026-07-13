"""database.misc — residual domain helpers (family-bot-8lx.6)."""
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

log = logging.getLogger("database.misc")

log = logging.getLogger(__name__)

async def prune_logs(retention_days: int = 30) -> dict:
    """Delete log rows older than retention_days. Returns counts of deleted rows per table."""
    cutoff = f"-{retention_days} days"
    counts = {}
    async with _db_conn() as db:
        for table, col in [
            ("activity_log",     "logged_at"),
            ("notification_log", "sent_at"),
            ("token_usage",      "logged_at"),
        ]:
            cur = await db.execute(
                f"DELETE FROM {table} WHERE {col} < datetime('now', ?)", (cutoff,)
            )
            counts[table] = cur.rowcount
        await db.commit()
    return counts

async def vacuum_db() -> None:
    """Reclaim disk space by rebuilding the SQLite file. Must run outside any transaction."""
    _p = _pkg()
    jm = sqlite_async.journal_mode_for_path(_resolve_db_path())
    vacuum_script = (
        f"PRAGMA journal_mode={jm};\n"
        "PRAGMA busy_timeout=5000;\n"  # per 40-SPEC Appendix A (1000-5000ms)
        "PRAGMA wal_checkpoint(TRUNCATE);\n"
        "VACUUM;\n"
    )
    max_retries = 3
    async with _get_lock():
        _c = getattr(_p, '_conn', None)
        if _c is not None:
            try:
                await sqlite_async.wal_checkpoint(_c, "TRUNCATE")
                await asyncio.to_thread(_c.close)
            except Exception:
                pass
            _p._conn = None
            _p._async_conn = None
            _p._conn_path = None

        for attempt in range(max_retries):
            try:
                async with sqlite_async.connect(_resolve_db_path(), timeout=120.0) as db:
                    await db.executescript(vacuum_script)
                log.info("SQLite VACUUM complete.")
                break
            except sqlite3.OperationalError as e:
                if "statements in progress" in str(e) and attempt < max_retries - 1:
                    wait_s = 5 * (attempt + 1)
                    log.warning(
                        "SQLite VACUUM failed (statements in progress), "
                        "retrying in %ds... (attempt %d/%d)",
                        wait_s, attempt + 1, max_retries,
                    )
                    await asyncio.sleep(wait_s)
                else:
                    raise
        else:
            return
    from database.schema import _set_last_vacuum_at
    await _set_last_vacuum_at()

NETWORK_DEVICES_PATH = "/data/network_devices.json"

async def save_network_devices_store(data: dict) -> None:
    """Persist network_devices.json (sole writer: cognition / monolith)."""
    import json
    import sys
    from pathlib import Path

    # Tests patch database.NETWORK_DEVICES_PATH on the package; read at call time.
    pkg = sys.modules.get("database")
    path_str = getattr(pkg, "NETWORK_DEVICES_PATH", NETWORK_DEVICES_PATH)
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_text, json.dumps(data, indent=2))

async def save_ha_devices(states: list[dict]) -> None:
    """Upsert a batch of raw HA state objects into ha_devices."""
    rows = []
    for s in states:
        eid = s.get("entity_id", "")
        if not eid:
            continue
        rows.append((
            eid,
            s.get("attributes", {}).get("friendly_name") or eid,
            eid.split(".")[0] if "." in eid else None,   # domain as type
            s.get("state"),
            s.get("last_updated"),
        ))
    if not rows:
        return
    async with _db_conn() as db:
        await db.executemany(
            """INSERT INTO ha_devices
               (entity_id, name, type, last_state, last_updated)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(entity_id) DO UPDATE SET
                 name=excluded.name,
                 type=excluded.type,
                 last_state=excluded.last_state,
                 last_updated=excluded.last_updated""",
            rows
        )
        await db.commit()

async def get_ha_devices() -> list[dict]:
    """Return all persisted HA device snapshots from the DB."""
    async with _db_conn() as db:

        async with db.execute("SELECT * FROM ha_devices") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_person_pref(person_id: str | None = None, discord_id: int | None = None) -> dict:
    """Read preferences by person_id (string) or discord_id (int). Returns defaults if not found.

    Short-lived read connection (family-bot-9bi / SPEC Appendix A) — does not take
    the write-conn asyncio.Lock used by mutations.
    """
    defaults = {
        "reminders_enabled": True, "dm_mode": True,
        "reminder_minutes": 30, "preferred_channels": "discord",
        "quiet_hours_start": None, "quiet_hours_end": None,
    }
    if not person_id and not discord_id:
        return defaults
    async with sqlite_async.connect(_resolve_db_path(), timeout=10.0) as db:
        if person_id:
            sql, val = "SELECT * FROM person_preferences WHERE person_id=?", (person_id,)
        else:
            sql, val = "SELECT * FROM person_preferences WHERE discord_id=?", (discord_id,)
        async with db.execute(sql, val) as cur:
            row = await cur.fetchone()
            if row:
                return {**defaults, **{k: bool(row[k]) if k in ("reminders_enabled", "dm_mode") else row[k]
                                       for k in row.keys()}}
            return defaults

async def set_person_pref(person_id: str, discord_id: int | None = None, **kwargs) -> None:
    """Upsert preference fields for a person. Pass only the fields you want to change."""
    allowed = {"reminders_enabled", "dm_mode", "reminder_minutes",
               "preferred_channels", "quiet_hours_start", "quiet_hours_end"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        async with db.execute(
            "SELECT 1 FROM person_preferences WHERE person_id=?", (person_id,)
        ) as cur:
            exists = await cur.fetchone()
        if exists:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            await db.execute(
                f"UPDATE person_preferences SET {set_clause} WHERE person_id=?",
                (*updates.values(), person_id)
            )
        else:
            cols = ["person_id"] + (["discord_id"] if discord_id else []) + list(updates.keys())
            vals = [person_id] + ([discord_id] if discord_id else []) + list(updates.values())
            await db.execute(
                f"INSERT INTO person_preferences ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                vals
            )
        await db.commit()

async def get_stale_ha_devices(stale_minutes: int = 60) -> list[dict]:
    """Return devices whose last_updated is older than stale_minutes ago."""
    async with _db_conn() as db:

        async with db.execute(
            """SELECT entity_id, name, last_state, last_updated
               FROM ha_devices
               WHERE last_updated < datetime('now', ?)
               ORDER BY last_updated ASC""",
            (f"-{stale_minutes} minutes",)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_history(channel_id: int, limit: int = 20, since: float | None = None) -> list[dict]:
    # IMPORTANT: the lexicographic `created_at >= since_iso` compare is only
    # safe because `add_message` (the only writer) stores values via
    # `datetime.now(dt_timezone.utc).isoformat()` — same `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`
    # shape used here. If a different writer ever lands on this table (e.g.
    # SQLite default `CURRENT_TIMESTAMP`), `T` vs space byte ordering will
    # silently filter out same-day rows. Keep both ends matched.
    #
    # Bypass _get_lock(): WAL allows concurrent reads while writers are active.
    # Using the shared-connection lock here means chat messages queue behind
    # write contention from background tasks (identity_service, presence, etc.)
    # and appear to hang. A short-lived read-only connection is safe and fast.
    async with sqlite_async.connect(_resolve_db_path(), timeout=10.0) as db:
        if since is not None:
            since_iso = datetime.fromtimestamp(since, tz=dt_timezone.utc).isoformat()
            cur = await db.execute(
                """SELECT role, content FROM conversation_history
                   WHERE channel_id=? AND created_at >= ? ORDER BY id DESC LIMIT ?""",
                (channel_id, since_iso, limit)
            )
        else:
            cur = await db.execute(
                """SELECT role, content FROM conversation_history
                   WHERE channel_id=? ORDER BY id DESC LIMIT ?""",
                (channel_id, limit)
            )
        rows = await cur.fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

async def add_message(channel_id: int, role: str, content: str):
    if not content or not content.strip():
        return
    async with _db_conn() as db:
        await db.execute(
            "INSERT INTO conversation_history (channel_id, role, content, created_at) VALUES (?,?,?,?)",
            (channel_id, role, content, datetime.now(dt_timezone.utc).isoformat())
        )
        await db.execute(
            """DELETE FROM conversation_history WHERE id NOT IN (
               SELECT id FROM conversation_history
               WHERE channel_id=? ORDER BY id DESC LIMIT 50)
               AND channel_id=?""",
            (channel_id, channel_id)
        )
        await db.commit()

async def is_reminder_sent(event_id: str, remind_min: int) -> bool:
    async with _db_conn() as db:
        cur = await db.execute(
            "SELECT 1 FROM sent_reminders WHERE event_id=? AND remind_min=?",
            (event_id, remind_min)
        )
        return await cur.fetchone() is not None

async def mark_reminder_sent(event_id: str, remind_min: int):
    async with _db_conn() as db:
        await db.execute(
            "INSERT OR IGNORE INTO sent_reminders (event_id, remind_min, sent_at) VALUES (?,?,?)",
            (event_id, remind_min, datetime.now(dt_timezone.utc).isoformat())
        )
        await db.commit()

async def store_message_mapping(message_id: int, event_id: str, event_title: str = None, message_type: str = "event"):
    async with _db_conn() as db:
        await db.execute(
            "INSERT OR REPLACE INTO message_event_map (message_id, event_id, event_title, message_type) VALUES (?, ?, ?, ?)",
            (message_id, event_id, event_title, message_type)
        )
        await db.commit()

async def get_mapping_for_message(message_id: int) -> dict | None:
    async with _db_conn() as db:
        async with db.execute(
            "SELECT event_id, event_title, message_type FROM message_event_map WHERE message_id = ?",
            (message_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return {"event_id": row[0], "event_title": row[1], "message_type": row[2]} if row else None

async def save_rsvp(event_id: str, discord_id: int, name: str, status: str):
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO rsvps (event_id, discord_id, name, status, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(event_id, discord_id)
               DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at""",
            (event_id, discord_id, name, status, datetime.now(dt_timezone.utc).isoformat())
        )
        await db.commit()

async def get_rsvps(event_id: str) -> list[dict]:
    async with _db_conn() as db:
        cur = await db.execute(
            "SELECT discord_id, name, status FROM rsvps WHERE event_id=? ORDER BY updated_at",
            (event_id,)
        )
        rows = await cur.fetchall()
        return [{"discord_id": r[0], "name": r[1], "status": r[2]} for r in rows]

async def get_active_insights(person_id: str) -> list[str]:
    """Return non-expired insights for a person (permanent or not yet expired)."""
    async with _db_conn() as db:
        now = datetime.now(dt_timezone.utc).isoformat()
        cur = await db.execute(
            """SELECT insight FROM family_insights
               WHERE person_id=? AND (is_permanent=1 OR expires_at > ?)
               ORDER BY generated_at DESC LIMIT 10""",
            (person_id.lower(), now)
        )
        rows = await cur.fetchall()
        return [r[0] for r in rows]

async def store_insights(person_id: str, insights: list[dict], source_date: str):
    """
    Insert a list of insight dicts for a person.

    Each dict must have:
      text (str)         — the insight sentence
      is_permanent (bool) — True to keep forever, False to expire
      expires_days (int)  — days until expiry when is_permanent=False (default 14)
    """
    from datetime import timedelta
    async with _db_conn() as db:
        for insight in insights:
            expires = None
            if not insight.get("is_permanent", False):
                days = insight.get("expires_days", 14)
                expires = (datetime.now(dt_timezone.utc) + timedelta(days=days)).isoformat()
            await db.execute(
                """INSERT INTO family_insights (person_id, insight, source_date, is_permanent, expires_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    person_id.lower(),
                    insight["text"],
                    source_date,
                    int(insight.get("is_permanent", False)),
                    expires
                )
            )
        await db.commit()

async def check_digest_exists(date_str: str) -> bool:
    """Return True if a completed digest exists for date_str (YYYY-MM-DD)."""
    async with _db_conn() as db:
        cur = await db.execute(
            "SELECT 1 FROM digest_log WHERE digest_date=? AND completed_at IS NOT NULL",
            (date_str,)
        )
        return await cur.fetchone() is not None

async def start_digest(date_str: str):
    """Insert a digest_log row for date_str with completed_at=NULL (marks in-progress)."""
    async with _db_conn() as db:
        await db.execute(
            "INSERT OR IGNORE INTO digest_log (digest_date) VALUES (?)",
            (date_str,)
        )
        await db.commit()

async def complete_digest(date_str: str):
    """Set completed_at for the digest_log row for date_str."""
    async with _db_conn() as db:
        await db.execute(
            "UPDATE digest_log SET completed_at=? WHERE digest_date=?",
            (datetime.now(dt_timezone.utc).isoformat(), date_str)
        )
        await db.commit()

async def get_cached_session_title(session_id: str) -> str | None:
    async with _db_conn() as db:
        cur = await db.execute("SELECT title FROM session_titles WHERE session_id = ?", (session_id,))
        row = await cur.fetchone()
        return row[0] if row else None

async def cache_session_title(session_id: str, title: str):
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            "INSERT OR REPLACE INTO session_titles (session_id, title, created_at) VALUES (?, ?, ?)",
            (session_id, title, now)
        )
        await db.commit()

async def create_chat_thread(thread_id: str, title: str, person_id: str):
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            "INSERT INTO chat_threads (id, title, person_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (thread_id, title, person_id, now, now)
        )
        await db.commit()

async def get_chat_threads(person_id: str):
    async with _db_conn() as db:

        cur = await db.execute(
            "SELECT * FROM chat_threads WHERE person_id = ? ORDER BY updated_at DESC",
            (person_id,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

async def get_chat_thread_messages(thread_id: str):
    async with _db_conn() as db:

        cur = await db.execute(
            "SELECT role, content, created_at FROM chat_messages WHERE thread_id = ? ORDER BY created_at ASC",
            (thread_id,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

async def add_chat_message(thread_id: str, role: str, content: str):
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            "INSERT INTO chat_messages (thread_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, role, content, now)
        )
        await db.execute(
            "UPDATE chat_threads SET updated_at = ? WHERE id = ?",
            (now, thread_id)
        )
        await db.commit()

async def update_chat_thread_title(thread_id: str, title: str):
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            "UPDATE chat_threads SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, thread_id)
        )
        await db.commit()

async def delete_chat_thread(thread_id: str):
    async with _db_conn() as db:
        await db.execute("DELETE FROM chat_messages WHERE thread_id = ?", (thread_id,))
        await db.execute("DELETE FROM chat_threads WHERE id = ?", (thread_id,))
        await db.commit()

async def get_unmined_chat_threads():
    async with _db_conn() as db:

        cur = await db.execute("SELECT * FROM chat_threads WHERE is_mined = 0")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

async def mark_chat_threads_mined(thread_ids: list[str]):
    async with _db_conn() as db:
        for tid in thread_ids:
            await db.execute("UPDATE chat_threads SET is_mined = 1 WHERE id = ?", (tid,))
        await db.commit()

async def delete_mined_chat_threads(days_old: int = 7):
    cutoff = (datetime.now(dt_timezone.utc) - timedelta(days=days_old)).isoformat()
    async with _db_conn() as db:
        # Get IDs of threads to delete
        cur = await db.execute(
            "SELECT id FROM chat_threads WHERE is_mined = 1 AND updated_at < ?",
            (cutoff,)
        )
        rows = await cur.fetchall()
        tids = [r[0] for r in rows]
        
        for tid in tids:
            await db.execute("DELETE FROM chat_messages WHERE thread_id = ?", (tid,))
            await db.execute("DELETE FROM chat_threads WHERE id = ?", (tid,))
        
        await db.commit()
        return len(tids)

async def add_observation(
    person_id: str,
    observation: str,
    source: str = "claude",
    confidence: float = 0.8,
    expires_at: str | None = None,
) -> int:
    """Store a distilled semantic observation about a person."""
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        cur = await db.execute(
            """INSERT INTO semantic_observations
               (person_id, observation, source, confidence, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (person_id.lower(), observation, source, confidence, now, expires_at),
        )
        await db.commit()
        return cur.lastrowid

async def get_observations(person_id: str, limit: int = 10) -> list[dict]:
    """Return active (non-expired) observations for a person, newest first."""
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:

        async with db.execute(
            """SELECT * FROM semantic_observations
               WHERE person_id=?
                 AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY created_at DESC LIMIT ?""",
            (person_id.lower(), now, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
        await db.commit()

async def upsert_tomorrow_context(
    for_date: str,
    summary: str,
    person_id: str | None = None,
    confidence: float | None = None,
    source_task_id: int | None = None,
) -> int:
    # SQLite treats NULL as distinct in UNIQUE constraints; coerce None → "" for
    # the household-level row so ON CONFLICT actually fires. Readers do the same.
    pid = (person_id or "").lower()
    async with _db_conn() as db:
        cur = await db.execute(
            """INSERT INTO tomorrow_context (for_date, person_id, summary, confidence, source_task_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(for_date, person_id) DO UPDATE SET
                 summary=excluded.summary,
                 confidence=excluded.confidence,
                 source_task_id=excluded.source_task_id,
                 created_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
            (for_date, pid, summary, confidence, source_task_id),
        )
        await db.commit()
        return cur.lastrowid

async def get_tomorrow_context(for_date: str, person_id: str | None = None) -> dict | None:
    pid = (person_id or "").lower()
    async with _db_conn() as db:

        async with db.execute(
            "SELECT * FROM tomorrow_context WHERE for_date=? AND person_id=?",
            (for_date, pid),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def upsert_routine(
    person_id: str,
    name: str,
    pattern: dict,
    confidence: float = 0.6,
    reinforce_bump: float = 0.1,
) -> int:
    """Insert a new routine OR reinforce an existing one.

    First insert sets confidence to the caller's value (clamped 0..1).
    Subsequent upserts bump confidence by `reinforce_bump` (clamped 0..0.3,
    capped at 1.0), increment times_observed, and refresh last_observed_at.
    """
    import json as _json
    now = datetime.now(dt_timezone.utc).isoformat()
    conf = max(0.0, min(1.0, float(confidence)))
    bump = max(0.0, min(0.3, float(reinforce_bump)))
    async with _db_conn() as db:
        cur = await db.execute(
            """INSERT INTO routines (person_id, name, pattern_json, confidence, last_observed_at, times_observed)
               VALUES (?, ?, ?, ?, ?, 1)
               ON CONFLICT(person_id, name) DO UPDATE SET
                 pattern_json=excluded.pattern_json,
                 confidence=MIN(1.0, routines.confidence + ?),
                 last_observed_at=excluded.last_observed_at,
                 times_observed=routines.times_observed + 1""",
            (person_id.lower(), name, _json.dumps(pattern), conf, now, bump),
        )
        await db.commit()
        return cur.lastrowid

async def get_routines(person_id: str | None = None, min_confidence: float = 0.0) -> list[dict]:
    async with _db_conn() as db:
        if person_id:
            q = """SELECT person_id, name, pattern_json, confidence, last_observed_at, times_observed
                   FROM routines
                   WHERE person_id = ? AND confidence >= ?
                   ORDER BY confidence DESC, times_observed DESC, name ASC"""
            args = (person_id.lower(), min_confidence)
        else:
            q = """SELECT person_id, name, pattern_json, confidence, last_observed_at, times_observed
                   FROM routines
                   WHERE confidence >= ?
                   ORDER BY confidence DESC, times_observed DESC, name ASC"""
            args = (min_confidence,)
        async with db.execute(q, args) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_semantic_observations(person_id: str | None = None, limit: int = 10) -> list[dict]:
    async with _db_conn() as db:
        if person_id:
            q = """SELECT person_id, observation, source, confidence, created_at, expires_at
                   FROM semantic_observations
                   WHERE person_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?"""
            args = (person_id.lower(), limit)
        else:
            q = """SELECT person_id, observation, source, confidence, created_at, expires_at
                   FROM semantic_observations
                   ORDER BY created_at DESC
                   LIMIT ?"""
            args = (limit,)
        async with db.execute(q, args) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def decay_routines(decay_per_run: float = 0.05) -> int:
    """Decay confidence of routines not reinforced in 14 days. Returns rows affected."""
    async with _db_conn() as db:
        cur = await db.execute(
            """UPDATE routines
               SET confidence = MAX(0.0, confidence - ?)
               WHERE last_observed_at < datetime('now', '-14 days')""",
            (decay_per_run,),
        )
        await db.commit()
        return cur.rowcount

async def get_recent_family_insights(days: int = 1, limit: int = 50, person_id: str | None = None) -> list[dict]:
    """Pull recent family_insights — optionally filtered to one person."""
    async with _db_conn() as db:

        if person_id:
            q = """SELECT person_id, insight, source_date, generated_at
                   FROM family_insights
                   WHERE generated_at > datetime('now', ?) AND person_id = ?
                   ORDER BY generated_at DESC LIMIT ?"""
            args = (f"-{days} days", person_id.lower(), limit)
        else:
            q = """SELECT person_id, insight, source_date, generated_at
                   FROM family_insights
                   WHERE generated_at > datetime('now', ?)
                   ORDER BY generated_at DESC LIMIT ?"""
            args = (f"-{days} days", limit)
        async with db.execute(q, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

_RESEARCH_THREAD_LOG_KEY = "thread:log"

async def get_host_ip_snapshot(host_id: str) -> dict | None:
    async with _db_conn() as db:
        db.row_factory = sqlite3.Row
        cur = await db.execute("SELECT * FROM host_ip_snapshots WHERE host_id=?", (host_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def upsert_host_ip_snapshot(
    host_id: str,
    ip: str,
    is_wired: bool,
    essid: str | None,
    *,
    is_online: bool = True,
) -> None:
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO host_ip_snapshots (host_id, ip, is_wired, essid, is_online, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(host_id) DO UPDATE SET
                 ip=excluded.ip, is_wired=excluded.is_wired, essid=excluded.essid,
                 is_online=excluded.is_online, updated_at=excluded.updated_at""",
            (host_id, ip, int(is_wired), essid, int(is_online), now),
        )
        await db.commit()

async def record_network_event(
    event_type: str,
    summary: str,
    *,
    host_id: str | None = None,
    severity: str = "info",
    details: str | None = None,
) -> dict:
    now = datetime.now(dt_timezone.utc).isoformat()
    async with _db_conn() as db:
        cur = await db.execute(
            """INSERT INTO network_events (event_type, host_id, severity, summary, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_type, host_id, severity, summary, details, now),
        )
        await db.commit()
        return {
            "id": cur.lastrowid,
            "event_type": event_type,
            "host_id": host_id,
            "severity": severity,
            "summary": summary,
            "details": details,
            "created_at": now,
        }

async def list_network_events(*, since: str | None = None, limit: int = 100) -> list[dict]:
    async with _db_conn() as db:
        db.row_factory = sqlite3.Row
        if since:
            cur = await db.execute(
                """SELECT * FROM network_events WHERE created_at >= ?
                   ORDER BY created_at ASC LIMIT ?""",
                (since, limit),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM network_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in await cur.fetchall()]

async def count_memory_events_by_person() -> dict[str, int]:
    async with _db_conn() as db:
        db.row_factory = sqlite3.Row
        async with db.execute(
            "SELECT person_id, COUNT(*) AS n FROM memory_events GROUP BY person_id"
        ) as cur:
            rows = await cur.fetchall()
        return {str(r["person_id"]).lower(): int(r["n"]) for r in rows}

async def list_memory_events(person_id: str, limit: int = 30) -> list[dict]:
    async with _db_conn() as db:
        db.row_factory = sqlite3.Row
        cur = await db.execute(
            """SELECT id, event_type, event_title, logged_at FROM memory_events
               WHERE person_id=? ORDER BY logged_at DESC LIMIT ?""",
            (person_id.lower(), limit),
        )
        rows = await cur.fetchall()
        return [
            {"id": r["id"], "event_type": r["event_type"], "title": r["event_title"], "logged_at": r["logged_at"]}
            for r in rows
        ]

async def delete_memory_event(person_id: str, event_id: int) -> None:
    async with _db_conn() as db:
        await db.execute(
            "DELETE FROM memory_events WHERE id=? AND person_id=?",
            (event_id, person_id.lower()),
        )
        await db.commit()

async def delete_memory_events_for_person(person_id: str) -> None:
    async with _db_conn() as db:
        await db.execute("DELETE FROM memory_events WHERE person_id=?", (person_id.lower(),))
        await db.commit()

async def insert_memory_event(person_id: str, event_type: str, event_title: str) -> None:
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO memory_events (person_id, event_type, event_title)
               VALUES (?, ?, ?)""",
            (person_id.lower(), event_type, event_title),
        )
        await db.commit()

async def get_memory_event_patterns(person_id: str) -> list[dict]:
    async with _db_conn() as db:
        db.row_factory = sqlite3.Row
        cur = await db.execute(
            """SELECT event_title, event_type, COUNT(*) as count
               FROM memory_events WHERE person_id=?
               GROUP BY event_title, event_type""",
            (person_id.lower(),),
        )
        rows = await cur.fetchall()
        return [{"title": r["event_title"], "type": r["event_type"], "count": r["count"]} for r in rows]

async def get_memory_behavior_since(person_id: str, since_iso: str) -> tuple[list, list]:
    async with _db_conn() as db:
        db.row_factory = sqlite3.Row
        cur = await db.execute(
            """SELECT event_title, COUNT(*) as count FROM memory_events
               WHERE person_id=? AND event_type='missed' AND logged_at >= ?
               GROUP BY event_title""",
            (person_id.lower(), since_iso),
        )
        missed = await cur.fetchall()
        cur = await db.execute(
            """SELECT event_title, COUNT(*) as count FROM memory_events
               WHERE person_id=? AND event_type='acknowledged' AND logged_at >= ?
               GROUP BY event_title""",
            (person_id.lower(), since_iso),
        )
        ack = await cur.fetchall()
        return missed, ack

async def prune_memory_events_before(cutoff_iso: str) -> int:
    async with _db_conn() as db:
        cur = await db.execute("DELETE FROM memory_events WHERE logged_at < ?", (cutoff_iso,))
        await db.commit()
        return cur.rowcount

async def person_preferences_row_exists(person_id: str) -> bool:
    async with _db_conn() as db:
        async with db.execute(
            "SELECT 1 FROM person_preferences WHERE person_id=?", (person_id,)
        ) as cur:
            return await cur.fetchone() is not None

async def maybe_maintenance_vacuum(*, max_age_days: int = 14) -> bool:
    """Run VACUUM on cognition startup or when last run exceeds max_age_days."""
    from database.schema import ensure_db_metadata_schema, get_db_metadata
    await ensure_db_metadata_schema()
    last = await get_db_metadata("last_vacuum_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=dt_timezone.utc)
            age = datetime.now(dt_timezone.utc) - last_dt
            if age < timedelta(days=max_age_days):
                return False
        except (ValueError, TypeError):
            pass
    await vacuum_db()
    return True

async def backup_db_vacuum_into(
    *,
    backup_dir: str | None = None,
    keep_days: int = 14,
) -> str | None:
    """family-bot-c79.5: VACUUM INTO a dated file; prune older backups.

    Returns backup path on success, None on skip/failure.
    """
    import logging
    from pathlib import Path

    _log = logging.getLogger(__name__)
    root = Path(backup_dir or os.environ.get("BERNIE_DB_BACKUP_DIR") or "/data/backups")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _log.error("backup_db: cannot create %s: %s", root, e)
        return None

    day = datetime.now(dt_timezone.utc).strftime("%Y%m%d")
    dest = root / f"family_bot-{day}.db"
    if dest.exists():
        _log.info("backup_db: already have %s", dest)
        return str(dest)

    dest_s = str(dest)
    # Escape single quotes for SQL string literal
    dest_sql = dest_s.replace("'", "''")

    # Fresh connection for VACUUM INTO (same pattern as vacuum_db)
    try:
        async with sqlite_async.connect(_resolve_db_path(), timeout=120.0) as db:
            await db.execute(f"VACUUM INTO '{dest_sql}'")
        _log.info("backup_db: VACUUM INTO %s", dest)
    except Exception as e:
        _log.error("backup_db: VACUUM INTO failed: %s", e)
        return None

    # Retention prune
    cutoff = datetime.now(dt_timezone.utc) - timedelta(days=max(1, keep_days))
    try:
        for p in root.glob("family_bot-*.db"):
            try:
                # parse YYYYMMDD from name
                stamp = p.stem.split("-", 1)[-1]
                dt = datetime.strptime(stamp, "%Y%m%d").replace(tzinfo=dt_timezone.utc)
                if dt < cutoff:
                    p.unlink(missing_ok=True)
                    _log.info("backup_db: pruned %s", p)
            except (ValueError, OSError):
                continue
    except OSError as e:
        _log.warning("backup_db: prune failed: %s", e)

    try:
        from database.schema import set_db_metadata
        await set_db_metadata("last_backup_at", datetime.now(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        pass
    return dest_s

def _person_id_for_discord(discord_id: int, name: str | None = None) -> str:
    """Map discord_id → canonical person_id via config; fallback to lowered name."""
    try:
        from config import config
        for pid, member in config.get("family_members", {}).items():
            if member.get("discord_id") == discord_id:
                return str(member.get("canonical_id") or pid).lower()
    except Exception:
        pass
    if name:
        return str(name).lower()
    return f"discord_{discord_id}"

async def prune_stale_low_confidence_routines(
    max_confidence: float = 0.3,
    stale_days: int = 30,
) -> list[dict]:
    """Delete routines with confidence < max_confidence and last seen older than stale_days.

    Returns the deleted rows for reporting. Staleness uses last_observed_at, falling
    back to created_at when last_observed_at is NULL.
    """
    days = max(1, int(stale_days))
    async with _db_conn() as db:
        async with db.execute(
            """SELECT person_id, name, confidence, last_observed_at, created_at
               FROM routines
               WHERE confidence < ?
                 AND COALESCE(last_observed_at, created_at) < datetime('now', ?)""",
            (float(max_confidence), f"-{days} days"),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            await db.execute(
                """DELETE FROM routines
                   WHERE confidence < ?
                     AND COALESCE(last_observed_at, created_at) < datetime('now', ?)""",
                (float(max_confidence), f"-{days} days"),
            )
            await db.commit()
        return rows

async def prune_stale_low_confidence_observations(
    max_confidence: float = 0.3,
    stale_days: int = 30,
) -> list[dict]:
    """Delete semantic_observations with confidence < max_confidence and created_at older than stale_days.

    Returns the deleted rows for reporting.
    """
    days = max(1, int(stale_days))
    async with _db_conn() as db:
        async with db.execute(
            """SELECT person_id, observation, confidence, created_at, source
               FROM semantic_observations
               WHERE confidence < ?
                 AND created_at < datetime('now', ?)""",
            (float(max_confidence), f"-{days} days"),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            await db.execute(
                """DELETE FROM semantic_observations
                   WHERE confidence < ?
                     AND created_at < datetime('now', ?)""",
                (float(max_confidence), f"-{days} days"),
            )
            await db.commit()
        return rows

async def create_pending_hitl(
    tool_name: str,
    args_json: str,
    ctx_json: str,
    *,
    reasoning: str | None = None,
    expires_at: str,
    requested_at: str,
) -> int:
    async with _db_conn() as db:
        cur = await db.execute(
            """INSERT INTO pending_hitl
               (status, tool_name, args_json, ctx_json, reasoning, requested_at, expires_at)
               VALUES ('pending', ?, ?, ?, ?, ?, ?)""",
            (tool_name, args_json, ctx_json, reasoning, requested_at, expires_at),
        )
        await db.commit()
        return cur.lastrowid

async def get_pending_hitl(pending_id: int) -> dict | None:
    async with _db_conn() as db:
        async with db.execute(
            "SELECT * FROM pending_hitl WHERE id = ?", (pending_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
            return None

async def list_pending_hitl(*, status: str = "pending") -> list[dict]:
    async with _db_conn() as db:
        async with db.execute(
            "SELECT * FROM pending_hitl WHERE status = ?", (status,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def resolve_pending_hitl(pending_id: int, decision: str, decided_by: str) -> bool:
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with _db_conn() as db:
        if decision == "denied":
            cur = await db.execute(
                """UPDATE pending_hitl
                   SET status = ?, decided_at = ?, decided_by = ?
                   WHERE id = ? AND status = 'pending'""",
                (decision, now_iso, decided_by, pending_id),
            )
        else:
            cur = await db.execute(
                """UPDATE pending_hitl
                   SET status = ?, decided_at = ?, decided_by = ?
                   WHERE id = ? AND status = 'pending' AND expires_at > ?""",
                (decision, now_iso, decided_by, pending_id, now_iso),
            )
        await db.commit()
        return cur.rowcount > 0

async def update_pending_hitl_notify_message_ids(pending_id: int, message_ids: str) -> None:
    async with _db_conn() as db:
        await db.execute(
            "UPDATE pending_hitl SET notify_message_ids = ? WHERE id = ?",
            (message_ids, pending_id)
        )
        await db.commit()

def parse_pending_hitl_notify_map(raw: str | None) -> dict[int, int]:
    """Parse notify_message_ids — admin Discord id → DM message id."""
    import json

    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if isinstance(data, dict):
        out: dict[int, int] = {}
        for key, val in data.items():
            try:
                out[int(key)] = int(val)
            except (TypeError, ValueError):
                continue
        return out
    return {}

async def set_pending_hitl_notify_message_ids(
    pending_id: int,
    entries: list[tuple[int, int]] | list[int],
) -> None:
    """Persist admin DM message ids — prefer ``[(admin_id, message_id), ...]`` map."""
    import json

    if entries and isinstance(entries[0], tuple):
        payload: dict | list = {str(admin_id): msg_id for admin_id, msg_id in entries}
    else:
        payload = entries
    async with _db_conn() as db:
        await db.execute(
            "UPDATE pending_hitl SET notify_message_ids = ? WHERE id = ?",
            (json.dumps(payload), pending_id),
        )
        await db.commit()

async def expire_stale_pending_hitl(now_iso: str) -> int:
    async with _db_conn() as db:
        cur = await db.execute(
            """UPDATE pending_hitl
               SET status = 'expired', decided_at = ?, decided_by = ?
               WHERE status = 'pending' AND expires_at <= ?""",
            (now_iso, "system:expiry", now_iso),
        )
        await db.commit()
        return cur.rowcount

async def purge_terminal_pending_hitl(older_than_iso: str) -> int:
    async with _db_conn() as db:
        cur = await db.execute(
            """DELETE FROM pending_hitl
               WHERE status IN ('approved', 'denied', 'expired') AND decided_at < ?""",
            (older_than_iso,),
        )
        await db.commit()
        return cur.rowcount

async def insert_email_signal(row: dict) -> int:
    import json as _json
    now = datetime.now(dt_timezone.utc).isoformat().replace("+00:00", "Z")
    topics = row.get("topics") or []
    if not isinstance(topics, str):
        topics = _json.dumps(topics)
    async with _db_conn() as db:
        cur = await db.execute(
            """INSERT INTO email_signals (
                   gmail_id, thread_id, received_at, subject, sender_email,
                   sender_person_id, forwarder_email, forwarder_person_id,
                   from_header, delivered_to_header, parse_confidence,
                   summary, topics, ingested_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["gmail_id"],
                row.get("thread_id"),
                row["received_at"],
                row.get("subject") or "",
                row.get("sender_email") or "",
                row.get("sender_person_id"),
                row.get("forwarder_email"),
                row.get("forwarder_person_id"),
                row.get("from_header"),
                row.get("delivered_to_header"),
                row.get("parse_confidence"),
                row.get("summary") or "",
                topics,
                row.get("ingested_at") or now,
            ),
        )
        await db.commit()
        return cur.lastrowid

async def get_email_signal_by_gmail_id(gmail_id: str) -> dict | None:
    async with _db_conn() as db:
        async with db.execute("SELECT * FROM email_signals WHERE gmail_id = ?", (gmail_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def get_email_signal_by_thread_id(thread_id: str) -> dict | None:
    if not thread_id:
        return None
    async with _db_conn() as db:
        async with db.execute(
            """SELECT * FROM email_signals
               WHERE thread_id = ? AND forwarder_email IS NOT NULL
               ORDER BY received_at DESC LIMIT 1""",
            (thread_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def search_email_signals(
    *,
    person_id: str | None = None,
    since_iso: str | None = None,
    limit: int = 15,
) -> list[dict]:
    clauses = ["1=1"]
    params: list = []
    if person_id:
        clauses.append(
            "(sender_person_id = ? OR forwarder_person_id = ?)"
        )
        params.extend([person_id, person_id])
    if since_iso:
        clauses.append("received_at >= ?")
        params.append(since_iso)
    params.append(limit)
    sql = (
        f"SELECT * FROM email_signals WHERE {' AND '.join(clauses)} "
        "ORDER BY received_at DESC LIMIT ?"
    )
    async with _db_conn() as db:
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def create_email_pending(
    *,
    recipient: str,
    subject: str,
    body: str,
    requester_id: str,
    requester_role: str,
    cc: list[str] | None = None,
    reply_to_gmail_id: str | None = None,
    thread_id: str | None = None,
) -> int:
    import json as _json
    now = datetime.now(dt_timezone.utc).isoformat().replace("+00:00", "Z")
    cc_json = _json.dumps(cc or [])
    async with _db_conn() as db:
        cur = await db.execute(
            """INSERT INTO email_pending
               (recipient, cc, subject, body, requester_id, requester_role,
                reply_to_gmail_id, thread_id, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,'pending',?)""",
            (
                recipient,
                cc_json,
                subject,
                body,
                requester_id,
                requester_role,
                reply_to_gmail_id,
                thread_id,
                now,
            ),
        )
        await db.commit()
        return cur.lastrowid

async def get_email_pending(pending_id: int) -> dict | None:
    async with _db_conn() as db:
        async with db.execute("SELECT * FROM email_pending WHERE id = ?", (pending_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def get_email_pending_by_message_id(message_id: int | str) -> dict | None:
    async with _db_conn() as db:
        async with db.execute(
            "SELECT * FROM email_pending WHERE smithy_message_id = ? AND status = 'pending'",
            (str(message_id),),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def update_email_pending_smithy_message(pending_id: int, message_id: int | str) -> None:
    async with _db_conn() as db:
        await db.execute(
            "UPDATE email_pending SET smithy_message_id = ? WHERE id = ?",
            (str(message_id), pending_id),
        )
        await db.commit()

async def resolve_email_pending(
    pending_id: int,
    *,
    status: str,
    decided_by: str | None = None,
    from_status: str = "pending",
) -> bool:
    now = datetime.now(dt_timezone.utc).isoformat().replace("+00:00", "Z")
    async with _db_conn() as db:
        cur = await db.execute(
            """UPDATE email_pending
               SET status = ?, decided_at = ?, decided_by = ?
               WHERE id = ? AND status = ?""",
            (status, now, decided_by, pending_id, from_status),
        )
        await db.commit()
        return cur.rowcount == 1

async def claim_email_pending_for_send(pending_id: int, *, decided_by: str) -> bool:
    """Atomically move pending → sending so only one approver can dispatch."""
    return await resolve_email_pending(
        pending_id, status="sending", decided_by=decided_by, from_status="pending"
    )

async def finalize_email_pending(
    pending_id: int,
    *,
    status: str,
    decided_by: str | None = None,
) -> bool:
    """Complete or revert a sending row."""
    return await resolve_email_pending(
        pending_id, status=status, decided_by=decided_by, from_status="sending"
    )

async def expire_stale_email_pending(older_than_iso: str) -> list[dict]:
    """Mark pending rows older than cutoff as expired; return rows updated."""
    now = datetime.now(dt_timezone.utc).isoformat().replace("+00:00", "Z")
    async with _db_conn() as db:
        async with db.execute(
            """SELECT id, smithy_message_id, requester_id, recipient
               FROM email_pending
               WHERE status = 'pending' AND created_at < ?""",
            (older_than_iso,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            return []
        await db.execute(
            """UPDATE email_pending
               SET status = 'expired', decided_at = ?, decided_by = 'system:expiry'
               WHERE status = 'pending' AND created_at < ?""",
            (now, older_than_iso),
        )
        await db.commit()
        return rows

async def get_email_ingest_history_id() -> str | None:
    async with _db_conn() as db:
        async with db.execute(
            "SELECT history_id FROM email_ingest_cursor WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else None

async def set_email_ingest_history_id(history_id: str) -> None:
    now = datetime.now(dt_timezone.utc).isoformat().replace("+00:00", "Z")
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO email_ingest_cursor (id, history_id, last_synced_at)
               VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET history_id = excluded.history_id,
                                             last_synced_at = excluded.last_synced_at""",
            (history_id, now),
        )
        await db.commit()

async def record_email_send(
    *,
    requester_id: str,
    recipient: str,
    recipient_domain: str,
    sent_at: str | None = None,
) -> None:
    ts = sent_at or datetime.now(dt_timezone.utc).isoformat().replace("+00:00", "Z")
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO email_send_rate (requester_id, recipient, recipient_domain, sent_at)
               VALUES (?,?,?,?)""",
            (requester_id, recipient.lower(), recipient_domain.lower(), ts),
        )
        await db.commit()

async def count_email_sends_since(
    *,
    requester_id: str | None = None,
    recipient_domain: str | None = None,
    since_iso: str,
) -> int:
    clauses = ["sent_at >= ?"]
    params: list = [since_iso]
    if requester_id:
        clauses.append("requester_id = ?")
        params.append(requester_id)
    if recipient_domain:
        clauses.append("recipient_domain = ?")
        params.append(recipient_domain.lower())
    sql = f"SELECT COUNT(*) FROM email_send_rate WHERE {' AND '.join(clauses)}"
    async with _db_conn() as db:
        async with db.execute(sql, params) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

async def prune_email_send_rate(older_than_iso: str) -> int:
    async with _db_conn() as db:
        cur = await db.execute(
            "DELETE FROM email_send_rate WHERE sent_at < ?",
            (older_than_iso,),
        )
        await db.commit()
        return cur.rowcount

