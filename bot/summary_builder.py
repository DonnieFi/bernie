# bot/summary_builder.py

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

@dataclass
class Highlight:
    emoji: str
    text: str
    urgency: int  # 1 (low) → 5 (high) — used to pick top 3
    kind: str = "event"


def build_highlights(events: list, weather_rec, garbage_tomorrow: bool, tz: ZoneInfo, school_cals: set = None) -> list[Highlight]:
    """
    Score all potential highlights and return top 3 by urgency.
    weather_rec is expected to be a WeatherRecommendation object from recommendation_engine
    """
    candidates: list[Highlight] = []
    now = datetime.now(tz)

    # --- Weather severity ---
    if weather_rec:
        severity = weather_rec.severity
        if severity == "high":
            candidates.append(Highlight("🌨️", weather_rec.summary, 5, "event"))
        elif severity == "medium":
            candidates.append(Highlight("🌦️", weather_rec.summary, 3, "event"))
        
        # Timing alerts as highlights if significant
        for alert in weather_rec.timing_alerts:
            if "Rain likely" in alert or "Snow" in alert:
                candidates.append(Highlight("☔", alert, 4, "event"))

    # --- Garbage day ---
    if garbage_tomorrow:
        candidates.append(Highlight("🗑️", "Garbage tomorrow — put bins out tonight", 4, "trash"))

    # --- Imminent events (within 4 hours) ---
    for event in events:
        if event.get("all_day"):
            continue
        event_time = event.get("start")
        if not event_time:
            continue
            
        minutes_away = (event_time - now).total_seconds() / 60
        if 0 < minutes_away < 120:
            candidates.append(Highlight(
                "⏰",
                f"{event['summary']} in {int(minutes_away)} min",
                5,
                "event"
            ))
        elif 0 < minutes_away < 240:
            candidates.append(Highlight(
                "📅",
                f"{event['summary']} at {event_time.strftime('%I:%M %p')}",
                3,
                "event"
            ))

    # --- First school class ---
    if school_cals:
        first_class = next(
            (e for e in sorted(
                [e for e in events if not e.get("all_day") and e.get("calendar_id") in school_cals],
                key=lambda e: e["start"]
            ) if (e["start"] - now).total_seconds() > -3600),
            None
        )
        if first_class:
            t = first_class["start"].strftime("%I:%M %p").lstrip("0")
            candidates.append(Highlight("🏫", f"{first_class['summary']} · {t}", 3, "school"))

    # Sort by urgency desc, take top 3
    candidates.sort(key=lambda h: h.urgency, reverse=True)
    
    # Dedup similar highlights if needed (e.g. redundant weather)
    seen_text = set()
    unique_highlights = []
    for h in candidates:
        if h.text not in seen_text:
            unique_highlights.append(h)
            seen_text.add(h.text)
            
    return unique_highlights[:3]


def format_highlights(highlights: list[Highlight]) -> str:
    if not highlights:
        return "Looks like a quiet day — nothing urgent."
    lines = []
    for h in highlights:
        lines.append(f"{h.emoji} {h.text}")
    return "\n".join(lines)
