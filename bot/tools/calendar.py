"""Calendar tool handlers.

Domain handler; dispatched via ToolGateway / llm.compat.execute_tool.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import uuid as _uuid

from tools import ROLE_ALL, ROLE_PARENTS, tool
from school_calendar import exclude_school_from_schedule, school_calendar_ids, show_school_in_daily_summary
import db_writes


def _no_calendar_service() -> str:
    return "Error: Calendar tools are not available in this context."


def _format_events(cal, events: list[dict], config: dict | None) -> str:
    gw = (config or {}).get("tool_gateway", {})
    if gw.get("calendar_summary_mode"):
        return cal.events_to_summary(events)
    return cal.events_to_text(events)


# ── get_todays_events ────────────────────────────────────────────────────────
@tool(
    name="get_todays_events",
    description="Get all calendar events for today. Always check before saying the day is clear.",
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_todays_events(args: dict, ctx) -> str:
    cal = ctx.services.calendar
    if cal is None:
        return _no_calendar_service()
    events = exclude_school_from_schedule(await cal.get_todays_events(), ctx.config)
    return _format_events(cal, events, ctx.config)


# ── get_week_events ──────────────────────────────────────────────────────────
@tool(
    name="get_week_events",
    description="Get all calendar events for the next 7 days. Use for 'anything coming up?' or 'am I free Thursday?' — scan for conflicts.",
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_week_events(args: dict, ctx) -> str:
    cal = ctx.services.calendar
    if cal is None:
        return _no_calendar_service()
    tomorrow = (
        datetime.now(ctx.services.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    events = exclude_school_from_schedule(
        await cal.get_events_starting(tomorrow, 7), ctx.config,
    )
    return _format_events(cal, events, ctx.config)


# ── get_month_events ─────────────────────────────────────────────────────────
@tool(
    name="get_month_events",
    description="Get all calendar events for the next 30 days.",
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_month_events(args: dict, ctx) -> str:
    cal = ctx.services.calendar
    if cal is None:
        return _no_calendar_service()
    tomorrow = (
        datetime.now(ctx.services.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    events = exclude_school_from_schedule(
        await cal.get_events_starting(tomorrow, 30), ctx.config,
    )
    return _format_events(cal, events, ctx.config)


# ── get_historical_events ────────────────────────────────────────────────────
@tool(
    name="get_historical_events",
    description=(
        "Look back at past calendar events. Use when asked about previous "
        "appointments, last visits, or anything historical ('when was the "
        "last...', 'did we...', 'how long ago...')."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "days_back": {
                "type": "integer",
                "description": "How many days back to search (default 90, max 365)",
            }
        },
        "required": [],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_historical_events(args: dict, ctx) -> str:
    cal = ctx.services.calendar
    if cal is None:
        return _no_calendar_service()
    raw = args.get("days_back")
    days_back = min(int(raw) if raw is not None else 90, 365)
    events = await cal.get_historical_events(days_back)
    if not events:
        return f"No events found in the last {days_back} days."
    return _format_events(cal, events, ctx.config)


# ── create_event (WRITE) ─────────────────────────────────────────────────────
@tool(
    name="create_event",
    description="Add a new event to the family calendar.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "summary":          {"type": "string",  "description": "Event title"},
            "date":             {"type": "string",  "description": "Date in YYYY-MM-DD format"},
            "time":             {"type": "string",  "description": "Start time in HH:MM 24h format"},
            "duration_minutes": {"type": "integer", "description": "Duration in minutes, default 60"},
            "attendees":        {"type": "array",   "items": {"type": "string"},
                                 "description": "Family member names to include"},
            "location":         {"type": "string",  "description": "Optional location"},
            "description":      {"type": "string",  "description": "Optional event description or notes"},
            "remind_minutes":   {"type": "array",   "items": {"type": "integer"},
                                 "description": "Custom reminder times in minutes before event, e.g. [30, 10]"},
        },
        "required": ["summary", "date", "time"],
    },
    role_required=ROLE_ALL,
    tier=2,
)
async def handle_create_event(args: dict, ctx) -> str:
    if ctx.shadow:
        return (
            f"[shadow: would have created event '{args.get('summary')}' "
            f"on {args.get('date')} at {args.get('time')}]"
        )
    db = ctx.services.db

    tz = ctx.services.tz
    naive = datetime.strptime(f"{args['date']} {args['time']}", "%Y-%m-%d %H:%M")
    start = naive.replace(tzinfo=tz)
    raw_duration = args.get("duration_minutes")
    duration = int(raw_duration) if raw_duration is not None else 60
    end = start + timedelta(minutes=duration)
    draft_id = f"draft_{_uuid.uuid4().hex[:8]}"
    await db_writes.routed("store_draft", draft_id, {
        "summary":        args["summary"],
        "start":          start.isoformat(),
        "end":            end.isoformat(),
        "attendees":      args.get("attendees", []),
        "location":       args.get("location", ""),
        "description":    args.get("description", ""),
        "remind_minutes": args.get("remind_minutes"),
    })
    when = start.strftime("%A %B %-d at %-I:%M %p")
    return (
        f"Draft ready for confirmation: **{args['summary']}** on {when}. "
        f"Awaiting ✅ in #smithy. (draft_id={draft_id})"
    )


# ── get_events_range ─────────────────────────────────────────────────────────
@tool(
    name="get_events_range",
    description=(
        "Get calendar events between two specific dates. Use when the user "
        "asks about a specific future window beyond this month, or a named "
        "week or month (e.g. 'the week of June 9th', 'anything in July')."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "Start date in YYYY-MM-DD format (inclusive)"},
            "end_date":   {"type": "string", "description": "End date in YYYY-MM-DD format (inclusive)"},
            "context":    {"type": "string", "description": "Why this range was chosen — logging only."},
        },
        "required": ["start_date", "end_date"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_events_range(args: dict, ctx) -> str:
    cal = ctx.services.calendar
    if cal is None:
        return _no_calendar_service()
    start_date = args["start_date"]
    end_date = args["end_date"]
    events = exclude_school_from_schedule(
        await cal.get_events_between(start_date, end_date), ctx.config,
    )
    if not events:
        return f"No events found between {start_date} and {end_date}."
    return _format_events(cal, events, ctx.config)


# ── get_rsvps ────────────────────────────────────────────────────────────────
@tool(
    name="get_rsvps",
    description="Get RSVP status for an event by partial name match.",
    input_schema={
        "type": "object",
        "properties": {
            "event_name": {"type": "string", "description": "Part of the event name to search for"}
        },
        "required": ["event_name"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_rsvps(args: dict, ctx) -> str:
    cal = ctx.services.calendar
    if cal is None:
        return _no_calendar_service()
    db = ctx.services.db

    events = await cal.get_events_for_days(14)
    name = args["event_name"].lower()
    matches = [e for e in events if name in e["summary"].lower()]
    if not matches:
        return f"No upcoming events found matching '{args['event_name']}'"
    event = matches[0]
    rsvps = await db.get_rsvps(event["id"])
    if not rsvps:
        return f"No RSVPs yet for '{event['summary']}'"
    lines = [f"RSVPs for {event['summary']}:"]
    for r in rsvps:
        emoji = {"yes": "✅", "no": "❌", "maybe": "🤔"}.get(r["status"], "❓")
        lines.append(f"  {emoji} {r['name']}")
    return "\n".join(lines)


# ── get_school_schedule ──────────────────────────────────────────────────────
@tool(
    name="get_school_schedule",
    description=(
        "Get Child1's school class schedule (timed periods). Use for 'what "
        "classes does Child1 have', 'what's her first class', 'what period "
        "does she have', 'what's her school day look like'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "day": {"type": "string", "description": "today (default), tomorrow, or YYYY-MM-DD"}
        },
        "required": [],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_school_schedule(args: dict, ctx) -> str:
    cal = ctx.services.calendar
    day_arg = args.get("day", "today").lower().strip()
    school_cals = school_calendar_ids(ctx.config)
    if day_arg == "tomorrow":
        events = await cal.get_tomorrows_events() if cal else []
        label = "Tomorrow"
    else:
        events = await cal.get_todays_events() if cal else []
        label = "Today"
    timed = sorted(
        [e for e in events if e.get("calendar_id") in school_cals and not e.get("all_day")],
        key=lambda e: e["start"],
    )
    if not timed:
        return f"No classes on the schedule for {label.lower()}."
    lines = [f"**{label}'s class schedule:**"]
    for e in timed:
        t = e["start"].strftime("%I:%M %p").lstrip("0")
        lines.append(f"• {t} — {e['summary']}")
    return "\n".join(lines)


# ── get_homework ─────────────────────────────────────────────────────────────
@tool(
    name="get_homework",
    description=(
        "Get Child1's homework, tests, and assignments due from the school "
        "calendar. Use for 'what's due', 'any homework', 'does she have a "
        "test', 'what assignments are coming up'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "timeframe": {"type": "string", "description": "today (default), tomorrow, or week"}
        },
        "required": [],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_homework(args: dict, ctx) -> str:
    cal = ctx.services.calendar
    timeframe = args.get("timeframe", "today").lower().strip()
    school_cals = school_calendar_ids(ctx.config)

    def _due(e):
        return e.get("due_date", e["end"] - timedelta(days=1))

    if timeframe == "week":
        events = await cal.get_week_events_from_monday() if cal else []
        all_day = [e for e in events if e.get("calendar_id") in school_cals and e.get("all_day")]
        by_day: dict = {}
        for e in all_day:
            d = _due(e).strftime("%A, %b %d")
            by_day.setdefault(d, []).append(e["summary"])
        if not by_day:
            return "Nothing due this week."
        lines = ["**Homework this week:**"]
        for day, items in sorted(by_day.items()):
            lines.append(f"**{day}:** {', '.join(items)}")
        return "\n".join(lines)

    if timeframe == "tomorrow":
        events = await cal.get_tomorrows_events() if cal else []
        target = date.today() + timedelta(days=1)
        label = "tomorrow"
    else:
        events = await cal.get_todays_events() if cal else []
        target = date.today()
        label = "today"

    due = [
        e
        for e in events
        if e.get("calendar_id") in school_cals
        and e.get("all_day")
        and _due(e).date() == target
    ]
    if not due:
        return f"Nothing due {label}."
    lines = [f"**Due {label}:**"] + [f"• {e['summary']}" for e in due]
    return "\n".join(lines)


# ── get_highlights ───────────────────────────────────────────────────────────
@tool(
    name="get_highlights",
    description=(
        "Get today's top 3 highlights ranked by urgency — combines weather severity, "
        "garbage collection alerts, and imminent calendar events. Use instead of calling "
        "weather, garbage, and calendar tools separately when a summary is needed."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_highlights(args: dict, ctx) -> str:
    cal = ctx.services.calendar
    if cal is None:
        return _no_calendar_service()

    from zoneinfo import ZoneInfo
    from weather_service import get_weather
    from recommendation_engine import get_recommendations
    from garbage_service import get_tomorrow_collection
    import summary_builder

    config = ctx.config
    session = ctx.services.session

    w = await get_weather(
        config.get("location", {}).get("lat", 44.6476),
        config.get("location", {}).get("lon", -63.5728),
        session,
    )
    rec = get_recommendations(w) if w else None
    events = exclude_school_from_schedule(await cal.get_todays_events(), config)
    tz_local = ZoneInfo(config.get("timezone", "America/Halifax"))
    garbage_tomorrow_evt = await get_tomorrow_collection(
        config.get("recollect_ics_url"), tz_local, session
    )
    summary_school_cals = (
        school_calendar_ids(config) if show_school_in_daily_summary(config) else set()
    )
    highlights = summary_builder.build_highlights(
        events,
        rec,
        garbage_tomorrow_evt is not None,
        tz_local,
        school_cals=summary_school_cals,
    )
    return summary_builder.format_highlights(highlights)


@tool(
    name="set_show_school_in_daily_summary",
    description=(
        "Toggle whether Child1's school calendar appears in the daily summary, "
        "morning schedule, and general calendar tools. Use for summer break "
        "(off) or when school resumes (on). Does not affect /school or get_school_schedule."
    ),
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "enabled": {
                "type": "boolean",
                "description": "true = show school in daily schedule; false = hide (summer break)",
            }
        },
        "required": ["enabled"],
    },
    role_required=ROLE_PARENTS,
    tier=2,
)
async def handle_set_show_school_in_daily_summary(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would set show_school_in_daily_summary={args.get('enabled')}]"
    enabled = bool(args.get("enabled"))
    from config import update_config

    await update_config({"show_school_in_daily_summary": enabled})
    state = "on" if enabled else "off"
    return (
        f"School calendar in daily summary is now **{state}**. "
        f"{'Class periods will appear in /summary and get_todays_events again.' if enabled else 'Summer mode: hidden from daily schedule; use /school or get_school_schedule to check classes.'}"
    )
