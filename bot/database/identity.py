"""database.identity — domain module (8lx.1 Phase 1)."""
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

log = logging.getLogger("database.identity")

async def list_unresolved_entities(limit: int = 20, min_count: int = 1) -> list[dict]:
    """Fetch unresolved entities from the database."""
    async with _db_conn() as db:
        async with db.execute(
            """SELECT entity_key, type, count, last_seen, context_snapshot
               FROM unresolved_entities
               WHERE count >= ?
               ORDER BY count DESC, last_seen DESC
               LIMIT ?""",
            (min_count, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def identity_health_check() -> bool:
    try:
        async with _db_conn() as db:
            await db.execute("SELECT 1 FROM identity_nodes LIMIT 1")
        return True
    except Exception:
        return False

async def identity_resolve_entity(key: str) -> dict | None:
    if not key:
        return None
    async with _db_conn() as db:
        await db.execute("PRAGMA foreign_keys = ON")
        db.row_factory = sqlite3.Row
        async with db.execute(
            """SELECT ia.confidence, ia.source, ia.verified, n.canonical_id
               FROM identity_aliases ia
               JOIN identity_nodes n ON ia.node_id = n.node_id
               WHERE LOWER(ia.alias) = LOWER(?)""",
            (str(key),),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return {
                    "canonical_id": row["canonical_id"],
                    "confidence": row["confidence"],
                    "source": row["source"],
                    "verified": bool(row["verified"]),
                }
        try:
            async with db.execute(
                """SELECT ia.confidence, ia.source, ia.verified, n.canonical_id
                   FROM identity_search s
                   JOIN identity_aliases ia ON s.alias = ia.alias
                   JOIN identity_nodes n ON ia.node_id = n.node_id
                   WHERE identity_search MATCH ?
                   ORDER BY rank LIMIT 1""",
                (str(key),),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return {
                        "canonical_id": row["canonical_id"],
                        "confidence": max(row["confidence"] * 0.8, 0.5),
                        "source": row["source"],
                        "verified": False,
                    }
        except Exception:
            pass
    return None

async def identity_get_node(canonical_id: str) -> dict | None:
    import json as _json
    if not canonical_id:
        return None
    async with _db_conn() as db:
        db.row_factory = sqlite3.Row
        async with db.execute(
            "SELECT * FROM identity_nodes WHERE canonical_id = ?", (canonical_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            meta = row["metadata"]
            return {
                "node_id": row["node_id"],
                "canonical_id": row["canonical_id"],
                "type": row["type"],
                "metadata": _json.loads(meta) if meta else {},
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

async def identity_list_aliases(canonical_id: str) -> list[dict]:
    async with _db_conn() as db:
        db.row_factory = sqlite3.Row
        async with db.execute(
            """SELECT ia.alias, ia.source, ia.verified, ia.created_at
               FROM identity_aliases ia
               JOIN identity_nodes n ON ia.node_id = n.node_id
               WHERE n.canonical_id = ? ORDER BY ia.created_at""",
            (canonical_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [
                {
                    "alias": r["alias"],
                    "source": r["source"],
                    "verified": bool(r["verified"]),
                    "added_at": r["created_at"],
                }
                for r in rows
            ]

async def identity_log_unresolved(entity_key: str, entity_type: str, context_json: str, now_iso: str) -> None:
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO unresolved_entities
               (entity_key, type, first_seen, last_seen, count, context_snapshot)
               VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(entity_key, type) DO UPDATE SET
                   last_seen = excluded.last_seen,
                   count = count + 1,
                   context_snapshot = excluded.context_snapshot""",
            (entity_key, entity_type, now_iso, now_iso, context_json),
        )
        await db.commit()

async def identity_upsert_node(
    canonical_id: str, node_type: str, meta_json: str, now_iso: str, node_id: str | None = None
) -> str:
    import uuid as _uuid
    async with _db_conn() as db:
        await db.execute("PRAGMA foreign_keys = ON")
        async with db.execute(
            "SELECT node_id FROM identity_nodes WHERE canonical_id = ?", (canonical_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                nid = row[0]
                await db.execute(
                    "UPDATE identity_nodes SET metadata=?, updated_at=? WHERE node_id=?",
                    (meta_json, now_iso, nid),
                )
            else:
                nid = node_id or str(_uuid.uuid4())
                await db.execute(
                    """INSERT INTO identity_nodes
                       (node_id, canonical_id, type, metadata, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (nid, canonical_id, node_type, meta_json, now_iso, now_iso),
                )
        await db.commit()
        return nid

async def identity_upsert_alias(
    alias_lc: str, node_id: str, confidence: float, source: str, verified: int, now_iso: str
) -> bool:
    async with _db_conn() as db:
        await db.execute("PRAGMA foreign_keys = ON")
        cur = await db.execute(
            """INSERT OR IGNORE INTO identity_aliases
               (alias, node_id, confidence, source, verified, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (alias_lc, node_id, confidence, source, verified, now_iso),
        )
        inserted = cur.rowcount > 0
        if inserted:
            try:
                await db.execute(
                    "INSERT INTO identity_search (alias, node_id) VALUES (?, ?)",
                    (alias_lc, node_id),
                )
            except Exception:
                pass
        await db.commit()
        return inserted

async def identity_upsert_edge(
    edge_id: str,
    source_id: str,
    target_id: str,
    rel_type: str,
    confidence: float,
    evidence: str,
    verified: int,
    now_iso: str,
) -> bool:
    async with _db_conn() as db:
        await db.execute("PRAGMA foreign_keys = ON")
        async with db.execute(
            """SELECT edge_id FROM identity_edges
               WHERE source_id = ? AND target_id = ? AND rel_type = ?""",
            (source_id, target_id, rel_type),
        ) as cur:
            if await cur.fetchone():
                return False
        await db.execute(
            """INSERT INTO identity_edges
               (edge_id, source_id, target_id, rel_type, confidence, evidence, verified, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (edge_id, source_id, target_id, rel_type, confidence, evidence, verified, now_iso),
        )
        await db.commit()
        return True

