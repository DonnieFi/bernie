"""Study event detection + idempotent enqueue.

Two surfaces:
  is_study_event(event, config) — pure predicate; True if the event title matches
    the configured keyword regex OR the event's calendar_id is in study_calendars.

  ensure_study_task(event, person_id) — checks cognitive_tasks for an existing
    queued/active/done study_guide task for this event_id; enqueues only when
    no such task is found and no dead_letter row exists within the cooldown
    window. Returns the new task_id or None.
"""
from __future__ import annotations

import re


def is_study_event(event: dict, config: dict) -> bool:
    cfg = (config.get("cognitive_workers") or {})
    keywords = cfg.get(
        "study_keywords",
        "test|exam|quiz|rehearsal|recital|audition|midterm|finals?|final exam",
    )
    allow_calendars = set(cfg.get("study_calendars", []) or [])

    cal_id = event.get("calendar_id") or event.get("calendar") or ""
    if cal_id and cal_id in allow_calendars:
        return True

    summary = event.get("summary") or event.get("title") or ""
    if not summary:
        return False
    try:
        return bool(re.search(keywords, summary, re.IGNORECASE))
    except re.error:
        # Bad regex in config — treat as no match rather than crashing the scanner
        return False


def event_dedup_key(event: dict) -> str | None:
    eid = event.get("id") or event.get("event_id") or event.get("ical_uid")
    if eid:
        return f"study_guide:{eid}"
    if event.get("summary") and event.get("start"):
        return f"study_guide:{event['summary']}@{event['start']}"
    return None


async def ensure_study_task(event: dict, person_id: str) -> int | None:
    """Enqueue a study_guide task for this event if none exists already.

    Returns new task_id, or None if a task is already queued/active/done.
    """
    from db_binding import get_database
    db = get_database()
    event_id = event.get("id") or event.get("event_id") or event.get("ical_uid")
    if not event_id:
        return None
    event_id_str = str(event_id)
    existing = await db.find_cognitive_task_by_payload_key(
        "study_guide", "event_id", event_id_str
    )
    if existing:
        return None
    if await db.has_recent_cognitive_dead_letter(
        "study_guide", "event_id", event_id_str, within_hours=24
    ):
        return None
    # Calendar events deliver `start`/`end` as datetime objects; the cognitive
    # task payload is JSON-serialized so anything non-primitive needs stringifying.
    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else (v or "")
    return await db.create_cognitive_task(
        type="study_guide",
        payload={
            "event_id": str(event_id),
            "person_id": (person_id or "").lower(),
            "summary": _iso(event.get("summary")) or "",
            "description": _iso(event.get("description")) or "",
            "start": _iso(event.get("start")),
            "end": _iso(event.get("end")),
            "location": _iso(event.get("location")) or "",
        },
        priority=5,
    )
