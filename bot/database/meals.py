"""database.meals — meals + groceries (family-bot-8lx.6)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone

from database.conn import _db_conn

log = logging.getLogger("database.meals")


async def get_meals(start_date: str, end_date: str) -> list[dict]:
    async with _db_conn() as db:
        cur = await db.execute(
            "SELECT date, meal_type, dish, notes FROM meals WHERE date >= ? AND date <= ? ORDER BY date, meal_type",
            (start_date, end_date)
        )
        rows = await cur.fetchall()
        return [{"date": r[0], "meal_type": r[1], "dish": r[2], "notes": r[3]} for r in rows]


async def set_meal(date: str, meal_type: str, dish: str, notes: str = ""):
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO meals (date, meal_type, dish, notes)
               VALUES (?,?,?,?)
               ON CONFLICT(date, meal_type)
               DO UPDATE SET dish=excluded.dish, notes=excluded.notes""",
            (date, meal_type, dish, notes)
        )
        await db.commit()


async def delete_meal(date: str, meal_type: str):
    async with _db_conn() as db:
        await db.execute("DELETE FROM meals WHERE date=? AND meal_type=?", (date, meal_type))
        await db.commit()


async def get_groceries() -> list[dict]:
    async with _db_conn() as db:
        cur = await db.execute("SELECT item, category FROM groceries ORDER BY category, added_at")
        rows = await cur.fetchall()
        return [{"item": r[0], "category": r[1]} for r in rows]


async def add_grocery(item: str, category: str = "Other"):
    async with _db_conn() as db:
        await db.execute(
            "INSERT INTO groceries (item, category, added_at) VALUES (?,?,?)",
            (item, category, datetime.now(dt_timezone.utc).isoformat())
        )
        await db.commit()


async def remove_grocery(item: str):
    async with _db_conn() as db:
        await db.execute("DELETE FROM groceries WHERE LOWER(item) = LOWER(?)", (item,))
        await db.commit()
