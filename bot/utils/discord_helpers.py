from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from constants import registry as person_registry

def person_display_name(person_id: str) -> str:
    return person_registry.display_name(person_id)

def weekday_num(raw: str) -> int:
    days = {
        "mon": 0, "monday": 0,
        "tue": 1, "tues": 1, "tuesday": 1,
        "wed": 2, "wednesday": 2,
        "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
        "fri": 4, "friday": 4,
        "sat": 5, "saturday": 5,
        "sun": 6, "sunday": 6,
    }
    key = raw.strip().lower()
    if key in days:
        return days[key]
    key3 = key[:3]
    if key3 in days:
        return days[key3]
    raise ValueError("Invalid weekday")


def parse_nl_schedule(text: str) -> tuple[str, dict] | None:
    """Parse light NL schedule phrases into (schedule_kind, payload).

    family-bot-5hy.12 — supports e.g.:
      every Sunday at 9am / sundays 09:00 / daily 7:30 / every day at 8:00
      every hour / hourly at :15 / once 2026-07-10T09:00
    Returns None if unrecognised (caller falls back to explicit kind+schedule).
    """
    import re
    from datetime import datetime

    raw = (text or "").strip()
    if not raw:
        return None
    t = raw.lower().strip()

    # once: ISO-ish datetime
    if t.startswith("once "):
        return "once", {"run_at": raw.split(None, 1)[1].strip()}
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if "T" in raw or re.match(r"\d{4}-\d{2}-\d{2}", raw):
            return "once", {"run_at": raw}
    except ValueError:
        pass

    # hourly
    m = re.search(r"(?:every\s+hour|hourly)(?:\s+at\s+:?(\d{1,2}))?", t)
    if m:
        minute = int(m.group(1) or 0)
        return "hourly", {"minute": minute}

    # daily / every day
    m = re.search(
        r"(?:every\s+day|daily|each\s+day)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        t,
    )
    if not m:
        m = re.search(r"^(\d{1,2}):(\d{2})\s*$", t)
        if m:
            return "daily", {"time": f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"}
    if m and m.lastindex and m.lastindex >= 1:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ap = (m.group(3) if m.lastindex >= 3 else None) or ""
        if ap == "pm" and hour < 12:
            hour += 12
        if ap == "am" and hour == 12:
            hour = 0
        return "daily", {"time": f"{hour:02d}:{minute:02d}"}

    # weekly: every Sunday at 9am / sundays 09:00
    m = re.search(
        r"(?:every\s+)?(mon|tue|wed|thu|fri|sat|sun)[a-z]*s?\s+"
        r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        t,
    )
    if m:
        dow = weekday_num(m.group(1))
        hour = int(m.group(2))
        minute = int(m.group(3) or 0)
        ap = (m.group(4) or "").lower()
        if ap == "pm" and hour < 12:
            hour += 12
        if ap == "am" and hour == 12:
            hour = 0
        return "weekly", {"day_of_week": dow, "time": f"{hour:02d}:{minute:02d}"}

    # cron: 5-field-ish
    if re.match(r"^[\d\*/, -]+$", t) and t.count(" ") >= 4:
        return "cron", {"expr": raw}

    return None

def next_automation_run(kind: str, payload: dict, tz_name: str, after_dt: datetime | None = None) -> datetime | None:
    base = after_dt or datetime.now(ZoneInfo(tz_name))
    if base.tzinfo is None:
        base = base.replace(tzinfo=ZoneInfo(tz_name))

    if kind == "once":
        run_at = payload.get("run_at")
        if not run_at:
            raise ValueError("once schedule requires run_at")
        dt = datetime.fromisoformat(run_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz_name))
        return dt if dt > base else None

    if kind == "hourly":
        minute = int(payload.get("minute", 0))
        if minute < 0 or minute > 59:
            raise ValueError("hourly minute must be 0-59")
        dt = base.replace(minute=minute, second=0, microsecond=0)
        if dt <= base:
            dt = dt + timedelta(hours=1)
        return dt

    if kind == "daily":
        hhmm = str(payload.get("time", "")).strip()
        hour, minute = map(int, hhmm.split(":"))
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("daily time must be HH:MM")
        dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= base:
            dt = dt + timedelta(days=1)
        return dt

    if kind == "weekly":
        raw_dow = payload.get("day_of_week")
        if raw_dow is None:
            return None  # malformed payload — caller should deactivate
        try:
            day_of_week = int(raw_dow)
        except (ValueError, TypeError):
            return None
        if not (0 <= day_of_week <= 6):
            return None
        hhmm = str(payload.get("time", "")).strip()
        if not hhmm or ":" not in hhmm:
            return None  # missing or unparseable time
        try:
            hour, minute = map(int, hhmm.split(":"))
        except (ValueError, TypeError):
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta_days = (day_of_week - dt.weekday()) % 7
        dt = dt + timedelta(days=delta_days)
        if dt <= base:
            dt = dt + timedelta(days=7)
        return dt

    if kind == "cron":
        expr = str(payload.get("expr", "")).strip()
        if not expr:
            raise ValueError("cron schedule requires expr")
        try:
            from croniter import croniter
        except Exception as e:
            raise ValueError("croniter is required for cron schedules") from e
        return croniter(expr, base).get_next(datetime)

    raise ValueError("Unsupported schedule kind")
