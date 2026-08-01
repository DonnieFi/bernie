# bot/recommendation_engine.py

from dataclasses import dataclass
from datetime import datetime
from datetime import datetime

@dataclass
class WeatherRecommendation:
    summary: str              # "Cold morning. Wear a jacket."
    clothing: list[str]       # ["jacket", "umbrella"]
    timing_alerts: list[str]  # ["Rain expected at 6pm"]
    severity: str             # "low" | "medium" | "high"


def get_recommendations(weather: dict) -> WeatherRecommendation:
    """
    weather dict expected keys:
      temp_c, feels_like_c, precip_prob_pct, precip_mm,
      wind_kph, condition, hourly (list of {hour, temp_c, precip_prob_pct})
    """
    clothing = []
    timing_alerts = []
    severity = "low"

    # Handle the case where weather might be missing keys
    temp = weather.get("feels_like_c", weather.get("temp_c", 10))
    precip_prob = weather.get("precip_prob_pct", 0)
    wind_kph = weather.get("wind_kph", 0)
    hourly = weather.get("hourly", [])

    # --- Clothing ---
    if temp < -10:
        clothing += ["heavy winter coat", "hat", "gloves", "warm boots"]
        severity = "high"
    elif temp < 0:
        clothing += ["winter coat", "gloves"]
        severity = "medium"
    elif temp < 8:
        clothing += ["jacket"]
        severity = "low"
    elif temp < 15:
        clothing += ["light jacket or layer"]

    if wind_kph > 40:
        clothing.append("windproof layer")
        if severity == "low":
            severity = "medium"

    if precip_prob > 60:
        clothing.append("umbrella")
        if severity == "low":
            severity = "medium"

    # --- Timing alerts from hourly data ---
    rain_flagged = False
    for hour_data in hourly:
        hour = hour_data.get("hour", 0)
        prob = hour_data.get("precip_prob_pct", 0)
        if prob > 60 and not rain_flagged:
            period = _hour_to_period(hour)
            timing_alerts.append(f"Rain likely {period} (~{prob}% chance)")
            rain_flagged = True

    if not rain_flagged and precip_prob < 20:
        timing_alerts.append("Dry day expected — good for being outside")

    # --- Summary line ---
    temp_display = weather.get("temp_c", temp)
    feels = weather.get("feels_like_c")
    feels_str = f", feels like {feels:.0f}°C" if feels and abs(feels - temp_display) > 2 else ""
    condition = weather.get("condition", "")

    summary = f"{condition} · {temp_display:.0f}°C{feels_str}."
    if clothing:
        summary += f" {_clothing_to_sentence(clothing)}."

    return WeatherRecommendation(
        summary=summary,
        clothing=clothing,
        timing_alerts=timing_alerts,
        severity=severity
    )


def _hour_to_period(hour: int) -> str:
    if hour < 12:
        return f"this morning ({hour}am)"
    elif hour == 12:
        return "at noon"
    elif hour < 17:
        return f"this afternoon ({hour - 12}pm)"
    elif hour < 21:
        return f"this evening ({hour - 12}pm)"
    else:
        return f"tonight ({hour - 12}pm)"


def _clothing_to_sentence(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return f"Bring a {items[0]}"
    return f"Bring: {', '.join(items[:-1])} and {items[-1]}"
