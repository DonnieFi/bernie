"""Shared calendar projections for Discord and web surfaces.

Wall rules: timed family/shared events go in the grid; timed school classes do
not. All-day school assignments feed the homework strip, while all-day
uniform/dress/PE/spirit events feed the uniform row. When
``show_school_in_daily_summary`` is off (summer mode), the wall API also hides
homework and uniform layers so the calendar matches daily schedule surfaces.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta
import re
from typing import Any

_UNIFORM_RE = re.compile(r"\b(uniform|dress|p\.?e\.?|phys(?:ical)?\s+ed|spirit)\b", re.I)
_DINNER_RE = re.compile(r"^dinner(?:\s*:|$)", re.I)
_GARBAGE_RE = re.compile(r"\b(garbage|recycl(?:e|ing)|green bin)\b", re.I)


def school_calendar_ids(config: dict | None) -> set[str]:
    """Normalize school_calendars config (string IDs or {id: ...} objects)."""
    raw = (config or {}).get("school_calendars") or []
    ids: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            ids.add(entry)
        elif isinstance(entry, dict):
            cid = entry.get("id") or entry.get("calendar_id")
            if cid:
                ids.add(str(cid))
    return ids


def _school_student(config: dict | None, calendar_id: str) -> str | None:
    for entry in (config or {}).get("school_calendars") or []:
        if isinstance(entry, dict) and (entry.get("id") or entry.get("calendar_id")) == calendar_id:
            return entry.get("student")
    return None


def event_person_ids(event: dict, config: dict | None) -> list[str]:
    """Return CalendarService owners, with school student attribution fallback."""
    members = (config or {}).get("family_members") or {}
    owners = [
        str((members.get(str(person)) or {}).get("canonical_id") or person)
        for person in event.get("attendees") or []
        if person
    ]
    student = _school_student(config, str(event.get("calendar_id") or ""))
    return owners or ([str(student)] if student else [])


def homework_due_events(
    events: list[dict],
    config: dict | None,
    start: date,
    end: date | None = None,
) -> list[dict]:
    """Select all-day school dues in the inclusive date range."""
    end = end or start
    school_ids = school_calendar_ids(config)
    return [
        event for event in events
        if event.get("calendar_id") in school_ids
        and event.get("all_day")
        and not _UNIFORM_RE.search(str(event.get("summary") or ""))
        and start <= event.get("due_date", event["end"] - timedelta(days=1)).date() <= end
    ]


def uniform_notes(events: list[dict], config: dict | None) -> list[dict]:
    """Project school all-day uniform/dress/PE/spirit notes by due day."""
    school_ids = school_calendar_ids(config)
    notes = []
    for event in events:
        if (
            event.get("calendar_id") not in school_ids
            or not event.get("all_day")
            or not _UNIFORM_RE.search(str(event.get("summary") or ""))
        ):
            continue
        due = event.get("due_date", event["end"] - timedelta(days=1))
        people = event_person_ids(event, config)
        notes.append({
            "date": due.date().isoformat(),
            "text": event.get("summary") or "",
            "person_id": people[0] if people else None,
        })
    return notes


def clean_event_description(desc: str | None) -> str:
    """Strip inline reminder tags from Google Calendar descriptions for display."""
    text = str(desc or "").strip()
    if "[remind:" in text:
        text = text.split("[remind:")[0].strip()
    return text


def events_to_wall(events: list[dict], config: dict | None) -> list[dict]:
    """Normalize CalendarService events for the read-only calendar wall."""
    school_ids = school_calendar_ids(config)
    projected = []
    for event in events:
        is_school = event.get("calendar_id") in school_ids
        title = str(event.get("summary") or "")
        if is_school or _DINNER_RE.search(title):
            continue
        people = event_person_ids(event, config)
        projected.append({
            "id": event.get("id"),
            "title": title,
            "start": _iso(event.get("start")),
            "end": _iso(event.get("end")),
            "all_day": bool(event.get("all_day")),
            "person_ids": people,
            "is_school": is_school,
            "location": event.get("location") or "",
            "description": clean_event_description(event.get("description")),
            "organizer": event.get("organizer_name") or "",
            "organizer_email": event.get("organizer_email") or "",
            "attendees": event.get("real_attendees") or [],
            "html_link": event.get("html_link") or "",
            "status": event.get("status") or "confirmed",
            "color_key": people[0] if people else ("school" if is_school else "family"),
            "is_garbage": bool(_GARBAGE_RE.search(title)),
        })
    return projected


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def show_school_in_daily_summary(config: dict | None) -> bool:
    """When false, hide school-calendar events from automatic/daily schedule surfaces."""
    return bool((config or {}).get("show_school_in_daily_summary", True))


def exclude_school_from_schedule(events: list[dict], config: dict | None) -> list[dict]:
    """Drop school-calendar rows when the daily-summary toggle is off."""
    if show_school_in_daily_summary(config):
        return events
    school_ids = school_calendar_ids(config)
    if not school_ids:
        return events
    return [e for e in events if e.get("calendar_id") not in school_ids]


def _event_start_key(ev: dict) -> Any:
    return ev.get("start") or 0


def select_daily_schedule(
    events: list[dict],
    config: dict | None,
) -> tuple[list[dict], list[dict]]:
    """Shared daily schedule selection for #smithy and the web Today card.

    Returns ``(display_events, school_events)`` where:

    * **display_events** — family events (timed + all-day) plus at most the
      first timed school class when school is shown (same rule as the embed).
    * **school_events** — full school partition when school is shown (for
      homework); empty when school is hidden.

    School toggle off: school calendars are excluded entirely (summer mode).
    """
    schedule_events = exclude_school_from_schedule(events, config)
    school_ids = school_calendar_ids(config)
    show_school = show_school_in_daily_summary(config)

    if not show_school or not school_ids:
        return sorted(schedule_events, key=_event_start_key), []

    school = [e for e in schedule_events if e.get("calendar_id") in school_ids]
    family = [e for e in schedule_events if e.get("calendar_id") not in school_ids]
    school_timed = sorted(
        [e for e in school if not e.get("all_day")],
        key=_event_start_key,
    )
    first_class = [school_timed[0]] if school_timed else []
    display = sorted(family + first_class, key=_event_start_key)
    return display, school


def _attendee_who(ev: dict) -> str:
    atts = [a for a in (ev.get("attendees") or []) if a]
    if not atts:
        return "family"
    if len(atts) == 1:
        return str(atts[0])
    return ", ".join(str(a) for a in atts)


def events_to_api_schedule(display_events: list[dict], tz) -> list[dict]:
    """Bucket display events for ``/api/today`` schedule card.

    Includes all-day events, any hour of day, and ``who`` from attendees so the
    web card matches #smithy (no 8–22 clip, no silent drop of all-day).
    """
    def _h_str(h: int) -> str:
        return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"

    buckets: OrderedDict[str, list] = OrderedDict()
    all_day = [e for e in display_events if e.get("all_day")]
    timed = [e for e in display_events if not e.get("all_day")]

    if all_day:
        buckets["All day"] = []
        for ev in all_day:
            buckets["All day"].append({
                "title": ev.get("summary") or "",
                "sub": ev.get("location") or "",
                "time": "All day",
                "who": _attendee_who(ev),
                "all_day": True,
            })

    for ev in timed:
        start = ev["start"]
        if hasattr(start, "tzinfo") and start.tzinfo is not None:
            start = start.astimezone(tz)
        key = _h_str(int(start.hour))
        buckets.setdefault(key, []).append({
            "title": ev.get("summary") or "",
            "sub": ev.get("location") or "",
            "time": start.strftime("%-I:%M %p").lower(),
            "who": _attendee_who(ev),
            "all_day": False,
        })

    return [{"hour": k, "events": v} for k, v in buckets.items()]
