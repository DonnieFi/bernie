"""Weather tool handlers."""
from __future__ import annotations

from tools import ROLE_ALL, tool


@tool(
    name="get_current_weather",
    description=(
        "Get weather for a city and day. ALWAYS call this for any weather or outdoor question — "
        "never answer from memory or the context snapshot. "
        "Omit city (or pass 'Halifax') for home weather; pass city name for elsewhere. "
        "Lead with severe weather (freezing rain, storm, high wind) when present. "
        "Lean into Halifax weather personality: 'Classic damp Halifax morning 🌫️'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "Optional city name, e.g. Toronto or Montreal"},
            "day":  {"type": "string", "description": "current, now, today, tomorrow, week, or specific"},
            "date": {"type": "string", "description": "YYYY-MM-DD when day is specific"},
        },
        "required": [],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_current_weather(args: dict, ctx) -> str:
    from weather_service import get_weather_for_request, format_weather_report
    city = args.get("city")
    day = args.get("day") or "today"
    date_str = args.get("date")
    report = await get_weather_for_request(city, day, ctx.services.session, date_str=date_str)
    return format_weather_report(report)
