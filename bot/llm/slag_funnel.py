"""Nudge open-ended planning threads toward #slag (perf P1)."""

from __future__ import annotations

import re
from typing import Any

_OPEN_ENDED_PATTERNS = [
    r"\bsleepover\b",
    r"\bfigure out\b",
    r"\bwork out\b",
    r"\bplan (a |the )?(party|weekend|trip|sleepover)\b",
    r"\bwhat if we\b",
    r"\bbrainstorm\b",
    r"\bhelp me plan\b",
    r"\blong thread\b",
    r"\bstep by step\b",
    r"\bcoordinate\b.*\b(friends|kids|parents)\b",
]

_SHORT_ACK = re.compile(
    r"^(ok|okay|thanks|thank you|yes|no|sure|got it|cool)\s*[!.?]*$",
    re.I,
)


def _channel_is(config: dict, channel_id: str | None, key: str) -> bool:
    if not channel_id:
        return False
    return str(channel_id) == str(config.get(f"{key}_channel_id", ""))


def should_suggest_slag(
    user_message: str,
    *,
    config: dict[str, Any],
    channel_id: str | None,
) -> bool:
    """True when Bernie should mention continuing in #slag."""
    funnel = (config.get("context") or {}).get("slag_funnel") or {}
    if not funnel.get("enabled", False):
        return False
    if _channel_is(config, channel_id, "slag"):
        return False
    if _channel_is(config, channel_id, "furnace"):
        return False
    text = (user_message or "").strip()
    if not text or _SHORT_ACK.match(text):
        return False
    lower = text.lower()
    return any(re.search(p, lower) for p in _OPEN_ENDED_PATTERNS)


def slag_funnel_system_block(config: dict[str, Any]) -> str:
    slag_id = config.get("slag_channel_id")
    channel_hint = f"<#{slag_id}>" if slag_id else "#slag"
    return (
        f"Open-ended planning note: if this becomes a long multi-step thread "
        f"(sleepovers, logistics, brainstorming), suggest continuing in {channel_hint} "
        f"for extended chat with a looser tool loop. Do not refuse to help in the "
        f"current channel — nudge only when the thread is clearly sprawling."
    )
