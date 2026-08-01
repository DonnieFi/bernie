"""Pure leg planner for context prefetch (no I/O).

Implements intent/mode/channel gates per perf_plan locked decisions.
Calendar: ``lazy`` = tools only (never in system prompt); ``intent`` = schedule
keywords on all channels; ``always`` = eager on non-furnace channels.
"""

from __future__ import annotations

from typing import Any


def _channel_is(config: dict, channel_id: str | None, key: str) -> bool:
    if not channel_id:
        return False
    cid = str(channel_id)
    return cid == str(config.get(f"{key}_channel_id", ""))


def looks_schedule_intent(text: str) -> bool:
    """Word-boundary-ish schedule intent (family-bot-2wh.13)."""
    if not text:
        return False
    import re

    t = text.lower()
    # Prefer phrases / word boundaries over bare substring "plan"/"free"/"class"
    patterns = [
        r"\btoday\b", r"\btomorrow\b", r"\bevents?\b", r"\bcalendar\b",
        r"\bschedule\b", r"\bschool\b", r"\bsleepover\b", r"\bappointment\b",
        r"\bmeeting\b", r"\bhomework\b", r"\bpractice\b",
        r"\bwhat(?:'s| is) on\b", r"\bfree (?:this|tomorrow|today|afternoon|evening)\b",
        r"\bbusy (?:this|tomorrow|today|afternoon|evening)\b",
        r"\bclass(?:es)?\b", r"\bgames?\b", r"\bmy plan\b", r"\bday plan\b",
    ]
    return any(re.search(p, t) for p in patterns)


def looks_home_intent(text: str) -> bool:
    """True when HA device dump is worth injecting (family-bot-2wh.10).

    Avoid bare \"home\"/\"room\" — too many false positives when prefetch.ha=intent.
    """
    if not text:
        return False
    t = text.lower()
    keys = (
        "light", "lamp", "switch", "tv", "media", "sonos", "thermostat",
        "climate", "device", "entity", "turn on", "turn off",
        "dim ", "brightness", "kitchen", "living room", "who's home",
        "who is home", "at home", "smart home",
    )
    return any(k in t for k in keys)


def calendar_prefetch_mode(config: dict[str, Any]) -> str:
    """Return ``context.prefetch.calendar`` (default ``intent`` for brownfield safety)."""
    prefetch = config.get("context", {}).get("prefetch", {}) or {}
    return str(prefetch.get("calendar", "intent")).lower()


def _calendar_prefetch_mode(config: dict[str, Any]) -> str:
    return calendar_prefetch_mode(config)


def should_prefetch_calendar(
    channel_id: str | None,
    is_dm: bool,
    mode: str | None,
    user_message: str | None,
    config: dict[str, Any],
) -> bool:
    """Return whether to inject calendar into the system prompt this turn."""
    m = str(mode or "").lower()
    if _channel_is(config, channel_id, "furnace") or "furnace" in m or "chef" in m:
        return False

    cal_mode = _calendar_prefetch_mode(config)
    if cal_mode in ("lazy", "never", "tools_only"):
        return False
    if cal_mode == "always":
        return True
    # intent (default for legacy configs): schedule keywords required everywhere
    if looks_schedule_intent(user_message or ""):
        return True
    prefetch = config.get("context", {}).get("prefetch", {})
    if is_dm and prefetch.get("dm_skip_calendar_default", True):
        return False
    if not is_dm:
        return False
    return True


def should_prefetch_weather(
    channel_id: str | None,
    is_dm: bool,
    mode: str | None,
    user_message: str | None,
    config: dict[str, Any],
) -> bool:
    """Return whether to prefetch weather."""
    m = str(mode or "").lower()
    if _channel_is(config, channel_id, "furnace") or "furnace" in m or "chef" in m:
        return False
    prefetch = config.get("context", {}).get("prefetch", {})
    mode_pref = prefetch.get("weather", "intent")
    if mode_pref == "always":
        return True
    if mode_pref == "never":
        return False
    if is_dm and user_message:
        t = user_message.lower()
        if any(k in t for k in ("weather", "rain", "snow", "forecast", "outside", "umbrella")):
            return True
        if looks_schedule_intent(user_message):
            return True
        return prefetch.get("dm_skip_weather_default", False) is False
    if user_message and not is_dm:
        t = user_message.lower()
        if any(k in t for k in ("weather", "rain", "snow", "forecast", "outside", "umbrella")):
            return True
        if looks_schedule_intent(user_message):
            return True
        weather_mode = str(prefetch.get("weather", "intent")).lower()
        if weather_mode == "always":
            return True
        return False
    # family-bot-2wh.13: intent mode defaults False (no empty-message weather prefetch)
    return False
