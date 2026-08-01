"""Read-only calendar wall and meal routes."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import api.common as _ac
from school_calendar import (
    event_person_ids,
    events_to_wall,
    exclude_school_from_schedule,
    homework_due_events,
    show_school_in_daily_summary,
    uniform_notes,
)


def _range(start: str, end: str) -> tuple[date, date]:
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="start and end must be YYYY-MM-DD") from exc
    if first > last:
        raise HTTPException(status_code=400, detail="start must be on or before end")
    return first, last


class SchoolScheduleToggle(BaseModel):
    enabled: bool


def _parent_roles() -> set[str]:
    return {"admin", "parents", "parent"}


def build_calendar_router(ctx: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/api/calendar", dependencies=[Depends(_ac.verify_token)])
    async def get_calendar(start: str = Query(...), end: str = Query(...)):
        first, last = _range(start, end)
        events = await ctx.calendar_service.get_events_between(start, end)
        show_school = show_school_in_daily_summary(_ac.config)
        school_layers = events if show_school else exclude_school_from_schedule(events, _ac.config)
        uniform_by_date: dict[str, list[dict]] = {}
        for note in uniform_notes(school_layers, _ac.config):
            uniform_by_date.setdefault(note.pop("date"), []).append(note)
        homework_by_date: dict[str, list[dict]] = {}
        for event in homework_due_events(school_layers, _ac.config, first, last):
            due = event.get("due_date", event["end"] - timedelta(days=1)).date().isoformat()
            homework_by_date.setdefault(due, []).append({
                "title": event.get("summary") or "",
                "person_ids": event_person_ids(event, _ac.config),
            })
        return {
            "events": events_to_wall(events, _ac.config),
            "uniform_by_date": uniform_by_date,
            "homework_by_date": homework_by_date,
            "show_school": show_school,
        }

    @router.patch("/api/calendar/school-schedule", dependencies=[Depends(_ac.verify_token)])
    async def set_school_schedule(
        body: SchoolScheduleToggle,
        user: _ac.Person = Depends(_ac.verify_token),
    ):
        if user.role not in _parent_roles():
            raise HTTPException(status_code=403, detail="parents or admin required")
        from config import update_config

        await update_config({"show_school_in_daily_summary": bool(body.enabled)})
        return {"ok": True, "show_school": bool(body.enabled)}

    return router
