"""Meals and grocery HTTP routes (family-bot-fqa9)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import api.common as _ac
import db_writes

_DISH_HINTS = {
    "taco": [("tortillas", "Bakery"), ("ground beef", "Meat"), ("cheese", "Dairy"), ("salsa", "Other")],
    "pasta": [("pasta", "Pantry"), ("tomato sauce", "Pantry"), ("parmesan", "Dairy")],
    "pizza": [("pizza dough", "Bakery"), ("mozzarella", "Dairy"), ("pepperoni", "Meat")],
    "salad": [("lettuce", "Produce"), ("cucumber", "Produce"), ("dressing", "Pantry")],
    "stir fry": [("rice", "Pantry"), ("soy sauce", "Pantry"), ("vegetables", "Produce")],
}


def _range(start: str, end: str) -> tuple[date, date]:
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="start and end must be YYYY-MM-DD") from exc
    if first > last:
        raise HTTPException(status_code=400, detail="start must be on or before end")
    return first, last


def _require_meal_editor(user: _ac.Person) -> None:
    if user.role not in {"admin", "parents", "parent"}:
        raise HTTPException(status_code=403, detail="Only parents/admin can edit meals")


def _mirror_enabled() -> bool:
    meals_cfg = (_ac.config or {}).get("meals") or {}
    return bool(meals_cfg.get("mirror_dinner_to_gcal", False))


async def _mirror_dinner(ctx: Any, day: str, dish: str, existing_id: str | None = None) -> str | None:
    if not _mirror_enabled() or not ctx.calendar_service:
        return existing_id
    cal_id = ((_ac.config or {}).get("meals") or {}).get("dinner_calendar_id") or "primary"
    end = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    body = {
        "summary": f"Dinner: {dish}",
        "start": {"date": day},
        "end": {"date": end},
    }

    def _sync():
        svc = ctx.calendar_service._get_service()
        if existing_id:
            return svc.events().patch(calendarId=cal_id, eventId=existing_id, body=body).execute()
        return svc.events().insert(calendarId=cal_id, body=body).execute()

    import asyncio
    event = await asyncio.get_running_loop().run_in_executor(None, _sync)
    ctx.calendar_service.invalidate_calendar_cache()
    return event.get("id")


async def _delete_mirrored_dinner(ctx: Any, event_id: str | None) -> None:
    if not event_id or not getattr(ctx, "calendar_service", None):
        return
    cal_id = ((_ac.config or {}).get("meals") or {}).get("dinner_calendar_id") or "primary"

    def _sync():
        return ctx.calendar_service._get_service().events().delete(
            calendarId=cal_id, eventId=event_id
        ).execute()

    import asyncio
    await asyncio.get_running_loop().run_in_executor(None, _sync)
    ctx.calendar_service.invalidate_calendar_cache()


class MealWrite(BaseModel):
    date: str
    meal_type: str = Field(default="dinner")
    dish: str
    notes: str = ""


class GroceryWrite(BaseModel):
    item: str
    category: str = "Other"


class GroceryChecked(BaseModel):
    checked: bool


class GroceryBulk(BaseModel):
    items: list[GroceryWrite]


def _suggest_from_meals(meals: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for meal in meals:
        dish = str(meal.get("dish") or "").lower()
        if not dish:
            continue
        for key, items in _DISH_HINTS.items():
            if key not in dish:
                continue
            for item, category in items:
                norm = item.lower()
                if norm in seen:
                    continue
                seen.add(norm)
                out.append({"item": item, "category": category, "from_dish": meal.get("dish")})
    return out


def build_meals_router(ctx: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/api/meals", dependencies=[Depends(_ac.verify_token)])
    async def get_meals(
        start: str = Query(...),
        end: str = Query(...),
        meal_type: str = Query("dinner"),
    ):
        _range(start, end)
        meals = await ctx.db.get_meals(start, end)
        wanted = meal_type.lower()
        return [meal for meal in meals if str(meal.get("meal_type") or "").lower() == wanted]

    @router.put("/api/meals", dependencies=[Depends(_ac.verify_token)])
    async def put_meal(body: MealWrite, user: _ac.Person = Depends(_ac.verify_token)):
        _require_meal_editor(user)
        _range(body.date, body.date)
        dish = body.dish.strip()
        if not dish:
            raise HTTPException(status_code=400, detail="dish is required")
        meal_type = body.meal_type.strip().lower() or "dinner"
        existing = await ctx.db.get_meals(body.date, body.date)
        prior = next((m for m in existing if m.get("meal_type") == meal_type), None)
        gcal_id = prior.get("gcal_event_id") if prior else None
        if meal_type == "dinner":
            try:
                gcal_id = await _mirror_dinner(ctx, body.date, dish, gcal_id)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"GCal mirror failed: {exc}") from exc
        await db_writes.routed(
            "set_meal", body.date, meal_type, dish, body.notes or "", gcal_event_id=gcal_id
        )
        return {
            "ok": True,
            "date": body.date,
            "meal_type": meal_type,
            "dish": dish,
            "notes": body.notes or "",
            "gcal_event_id": gcal_id,
        }

    @router.delete("/api/meals", dependencies=[Depends(_ac.verify_token)])
    async def delete_meal(
        date: str = Query(...),
        meal_type: str = Query("dinner"),
        user: _ac.Person = Depends(_ac.verify_token),
    ):
        _require_meal_editor(user)
        _range(date, date)
        wanted = meal_type.strip().lower() or "dinner"
        existing = await ctx.db.get_meals(date, date)
        prior = next((m for m in existing if m.get("meal_type") == wanted), None)
        if wanted == "dinner" and prior:
            try:
                await _delete_mirrored_dinner(ctx, prior.get("gcal_event_id"))
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"GCal mirror failed: {exc}") from exc
        await db_writes.routed("delete_meal", date, wanted)
        return {"ok": True}

    @router.post("/api/meals/suggest-groceries", dependencies=[Depends(_ac.verify_token)])
    async def suggest_groceries(
        start: str = Query(...),
        end: str = Query(...),
        meal_type: str = Query("dinner"),
    ):
        _range(start, end)
        meals = await ctx.db.get_meals(start, end)
        wanted = meal_type.lower()
        filtered = [m for m in meals if str(m.get("meal_type") or "").lower() == wanted]
        return {"suggestions": _suggest_from_meals(filtered)}

    @router.get("/api/groceries", dependencies=[Depends(_ac.verify_token)])
    async def get_groceries():
        return await ctx.db.get_groceries()

    @router.post("/api/groceries", dependencies=[Depends(_ac.verify_token)])
    async def add_grocery(body: GroceryWrite, user: _ac.Person = Depends(_ac.verify_token)):
        item = body.item.strip()
        if not item:
            raise HTTPException(status_code=400, detail="item is required")
        category = (body.category or "Other").strip() or "Other"
        await db_writes.routed("add_grocery", item, category)
        return {"ok": True, "item": item, "category": category}

    @router.post("/api/groceries/bulk", dependencies=[Depends(_ac.verify_token)])
    async def add_groceries_bulk(body: GroceryBulk, user: _ac.Person = Depends(_ac.verify_token)):
        added = []
        for row in body.items:
            item = row.item.strip()
            if not item:
                continue
            category = (row.category or "Other").strip() or "Other"
            await db_writes.routed("add_grocery", item, category)
            added.append({"item": item, "category": category})
        return {"ok": True, "added": added}

    @router.patch("/api/groceries/{grocery_id}", dependencies=[Depends(_ac.verify_token)])
    async def patch_grocery(
        grocery_id: int,
        body: GroceryChecked,
        user: _ac.Person = Depends(_ac.verify_token),
    ):
        await db_writes.routed("set_grocery_checked", grocery_id, body.checked)
        return {"ok": True, "id": grocery_id, "checked": body.checked}

    @router.delete("/api/groceries", dependencies=[Depends(_ac.verify_token)])
    async def remove_grocery(item: str = Query(...), user: _ac.Person = Depends(_ac.verify_token)):
        cleaned = item.strip()
        if not cleaned:
            raise HTTPException(status_code=400, detail="item is required")
        await db_writes.routed("remove_grocery", cleaned)
        return {"ok": True}

    return router
