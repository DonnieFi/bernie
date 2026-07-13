"""database.notifications — notification_log + pending queue (family-bot-8lx.6)."""
from __future__ import annotations

import logging

from database.conn import _db_conn, _db_read

log = logging.getLogger("database.notifications")


async def log_notification(recipient_id: str, channel: str, message: str, success: bool, error: str = None):
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO notification_log (recipient_id, channel, message, success, error)
               VALUES (?, ?, ?, ?, ?)""",
            (recipient_id, channel, message, int(success), error)
        )
        await db.commit()


async def add_pending_notification(
    recipient_id: str,
    message: str = None,
    title: str = None,
    embed_json: str = None,
    urgency: str = "normal",
    event_id: str = None,
    message_type: str = None,
):
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO pending_notifications
               (recipient_id, message, title, embed_json, urgency, event_id, message_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (recipient_id, message, title, embed_json, urgency, event_id, message_type)
        )
        await db.commit()


async def list_pending_notifications(recipient_id: str) -> list[dict]:
    async with _db_read() as db:
        cur = await db.execute(
            "SELECT * FROM pending_notifications WHERE recipient_id = ? ORDER BY created_at ASC",
            (recipient_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def list_pending_recipients() -> list[str]:
    """Distinct recipient_ids that currently have queued notifications."""
    async with _db_read() as db:
        cur = await db.execute("SELECT DISTINCT recipient_id FROM pending_notifications")
        rows = await cur.fetchall()
        return [r["recipient_id"] for r in rows]


async def clear_pending_notifications(recipient_id: str):
    async with _db_conn() as db:
        await db.execute("DELETE FROM pending_notifications WHERE recipient_id = ?", (recipient_id,))
        await db.commit()


async def clear_pending_notifications_by_ids(ids: list[int]):
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    async with _db_conn() as db:
        await db.execute(f"DELETE FROM pending_notifications WHERE id IN ({placeholders})", ids)
        await db.commit()


async def get_notification_log(limit: int = 20):
    async with _db_read() as db:
        cur = await db.execute(
            """SELECT recipient_id, message, channel, success, sent_at
               FROM notification_log ORDER BY sent_at DESC LIMIT ?""",
            (limit,)
        )
        rows = await cur.fetchall()
        return [
            {"who": r[0], "msg": r[1], "chan": r[2], "status": "sent" if r[3] else "fail", "time": r[4]}
            for r in rows
        ]
