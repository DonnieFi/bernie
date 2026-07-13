"""database.drafts — pending calendar drafts (family-bot-8lx.6)."""
from __future__ import annotations

import logging
from datetime import datetime

from database.conn import _db_conn, _db_read

log = logging.getLogger("database.drafts")


async def store_draft(draft_id: str, draft: dict):
    import json

    def _iso(val):
        if val is None:
            return None
        if isinstance(val, str):
            return val
        if isinstance(val, datetime):
            return val.isoformat()
        iso = getattr(val, "isoformat", None)
        if callable(iso):
            try:
                return iso()
            except TypeError:
                return str(val)
        return str(val)

    async with _db_conn() as db:
        await db.execute(
            """INSERT OR REPLACE INTO pending_drafts
               (draft_id, summary, start_time, end_time, attendees, location, description, remind_minutes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                draft_id,
                draft["summary"],
                _iso(draft["start"]),
                _iso(draft["end"]),
                json.dumps(draft.get("attendees", [])),
                draft.get("location", ""),
                draft.get("description", ""),
                json.dumps(draft["remind_minutes"]) if draft.get("remind_minutes") is not None else None,
            )
        )
        await db.commit()


def _row_to_draft(row) -> dict:
    import json
    return {
        "draft_id": row["draft_id"],
        "summary": row["summary"],
        "start": datetime.fromisoformat(row["start_time"]),
        "end": datetime.fromisoformat(row["end_time"]),
        "attendees": json.loads(row["attendees"] or "[]"),
        "location": row["location"] or "",
        "description": row["description"] or "",
        "remind_minutes": json.loads(row["remind_minutes"]) if row["remind_minutes"] else None,
        "posted": bool(row["posted"]),
    }


async def get_draft(draft_id: str) -> dict | None:
    async with _db_read() as db:
        cur = await db.execute(
            "SELECT * FROM pending_drafts WHERE draft_id=?",
            (draft_id,),
        )
        row = await cur.fetchone()
    return _row_to_draft(row) if row else None


async def get_unposted_drafts() -> list[dict]:
    async with _db_read() as db:
        cur = await db.execute(
            "SELECT * FROM pending_drafts WHERE posted=0 ORDER BY created_at ASC"
        )
        rows = await cur.fetchall()
    return [_row_to_draft(row) for row in rows]


async def mark_draft_posted(draft_id: str):
    async with _db_conn() as db:
        await db.execute("UPDATE pending_drafts SET posted=1 WHERE draft_id=?", (draft_id,))
        await db.commit()


async def delete_draft(draft_id: str):
    async with _db_conn() as db:
        await db.execute("DELETE FROM pending_drafts WHERE draft_id=?", (draft_id,))
        await db.commit()


async def cleanup_old_drafts(max_age_hours: int = 48) -> int:
    async with _db_conn() as db:
        cur = await db.execute(
            "DELETE FROM pending_drafts WHERE created_at < datetime('now', ?)",
            (f"-{max_age_hours} hours",)
        )
        await db.commit()
        return cur.rowcount
