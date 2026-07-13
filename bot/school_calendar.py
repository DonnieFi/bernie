"""School calendar helpers — summer/break toggle for daily schedule views."""

from __future__ import annotations


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
