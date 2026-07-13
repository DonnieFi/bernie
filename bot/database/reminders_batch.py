"""Batch reminder / preference reads (family-bot-ah5.3).

Keeps the minute reminder loop off the N+1 is_reminder_sent + get_person_pref path.
"""
from __future__ import annotations

import sqlite_async

from database.conn import _resolve_db_path


async def filter_unsent_reminders(pairs: list[tuple[str, int]]) -> set[tuple[str, int]]:
    """Return (event_id, remind_min) pairs that are NOT yet in sent_reminders."""
    if not pairs:
        return set()
    uniq = list(dict.fromkeys((str(eid), int(rm)) for eid, rm in pairs))
    sent: set[tuple[str, int]] = set()
    async with sqlite_async.connect(_resolve_db_path(), timeout=10.0) as db:
        for i in range(0, len(uniq), 200):
            chunk = uniq[i : i + 200]
            clause = " OR ".join("(event_id=? AND remind_min=?)" for _ in chunk)
            flat: list = []
            for eid, rm in chunk:
                flat.extend((eid, rm))
            async with db.execute(
                f"SELECT event_id, remind_min FROM sent_reminders WHERE {clause}",
                flat,
            ) as cur:
                async for row in cur:
                    sent.add((str(row[0]), int(row[1])))
    return set(uniq) - sent


async def get_person_prefs_by_discord_ids(discord_ids: list[int]) -> dict[int, dict]:
    """Batch-load person_preferences keyed by discord_id (defaults if missing)."""
    defaults = {
        "reminders_enabled": True, "dm_mode": True,
        "reminder_minutes": 30, "preferred_channels": "discord",
        "quiet_hours_start": None, "quiet_hours_end": None,
    }
    ids = sorted({int(d) for d in discord_ids if d is not None and int(d) != 0})
    if not ids:
        return {}
    out = {i: dict(defaults) for i in ids}
    placeholders = ",".join("?" * len(ids))
    async with sqlite_async.connect(_resolve_db_path(), timeout=10.0) as db:
        async with db.execute(
            f"SELECT * FROM person_preferences WHERE discord_id IN ({placeholders})",
            ids,
        ) as cur:
            async for row in cur:
                d = dict(row)
                did = d.get("discord_id")
                if did is None:
                    continue
                did = int(did)
                out[did] = {
                    **defaults,
                    **{
                        k: bool(d[k]) if k in ("reminders_enabled", "dm_mode") else d[k]
                        for k in d.keys()
                    },
                }
    return out
