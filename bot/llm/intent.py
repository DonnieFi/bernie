"""Intent detection helpers (live-data vs multistep) for routing.

Carved from claude_service.py in Phase 4.4 Session 0.
These must remain dependency-light: no import of claude_service.
The public names (no leading _) are used by llm/routing and re-exported
from the claude_service facade for test/compat continuity.
"""

import re

# Multi-step intent signals — comparison, aggregation, and chaining. A query
# matching any of these needs the model to filter/compare/branch on tool results
# (2+ decisions), which is smol's domain; everything else is a simple lookup that
# native answers faster. Tunable via `executor.smol_intent_patterns` (replaces
# this list). Misroutes are non-catastrophic: native synthesizes a real answer,
# and smol handles simple lookups too (just slower).
DEFAULT_SMOL_PATTERNS = [
    r"\bcompare\b", r"\bvs\.?\b", r"\bversus\b",
    r"\bdifference(s)?\b", r"\bdiffer\b",
    r"\bbetween\b.*\band\b", r"\bboth\b",
    r"\bcross[- ]?reference\b", r"\breconcile\b",
    r"\beach\b", r"\bevery(one|body|thing)?\b", r"\ball of\b", r"\bacross\b",
    r"\bplan\b", r"\bfigure out\b", r"\bwork out\b",
    r"\band then\b", r"\bafter that\b",
]

# Live-data intent signals — vehicle, health wearables, network, locks. These
# are single-shot tool lookups that native answers faster than smol. Tunable
# via `executor.native_intent_patterns` (replaces this list when non-empty).
DEFAULT_NATIVE_INTENT_PATTERNS = [
    r"\bniro\w*",
    r"\block\b", r"\bunlock\b", r"\blocked\b",
    r"\b(car|vehicle)\b.*\b(lock|unlock|battery|charge|charging|odometer|mileage|status)\b",
    r"\b(lock|unlock|battery|charge|charging|odometer|mileage|status)\b.*\b(car|vehicle|door)\b",
    r"\bbattery level\b", r"\bbody battery\b",
    r"\b(charging|charge) status\b", r"\bodometer\b",
    r"\bgarmin\b.*\b(sleep|hrv|battery|steps|stress|heart rate|resting)\b",
    r"\b(sleep|hrv|battery|steps|stress|heart rate|resting)\b.*\bgarmin\b",
    r"\boura\b.*\b(sleep|hrv|readiness|score)\b",
    r"\b(sleep|hrv|readiness|score)\b.*\boura\b",
    r"\bsleep score\b",     r"\bhow (did|have|was|well).*\bsleep",
    r"\b(last night|yesterday).*\bsleep\b",
    r"\bcompare\b.*\b(oura|garmin|sleep|tracker|ring)\b",
    r"\b(oura|garmin)\b.*\bcompare\b",
    r"\bhrv\b",
    r"\bpihole\b", r"\bpi-hole\b",
    r"\bnetwork status\b", r"\bunifi\b",
    r"\bdoor lock\b",
    r"\bhalifax transit\b",
    r"\broute\s+\d+\b",
    r"\bbus\s+\d+\b",
    r"\bnumber\s+\d+\b.*\bbus\b",
    r"\bbus\b.*\bnumber\s+\d+\b",
    r"\btrack(ing)?\b.*\bbus\s+\d+",
    r"\bwhere('?s| is)\b.*\bbus\s+\d+",
    r"\b/bus\b",
]


def looks_multistep(user_message: str, app_config: dict) -> bool:
    """Heuristic: does this query need multi-step reasoning (-> smol) vs a simple
    lookup (-> native)? Pattern-based (zero added latency/cost), config-tunable."""
    if not user_message:
        return False
    text = user_message.lower()
    patterns = app_config.get("executor", {}).get("smol_intent_patterns") or DEFAULT_SMOL_PATTERNS
    if any(re.search(p, text) for p in patterns):
        return True
    return text.count("?") >= 2


def looks_live_data(user_message: str, app_config: dict) -> bool:
    """Heuristic: does this query need a live tool lookup (-> native) even when
    the chat surface default is smol? Pattern-based, config-tunable.

    Delegates to health_sleep.looks_health_sleep_query for wearable/sleep
    patterns (including custom executor.health_sleep_patterns) so that
    Garmin/Oura queries force native routing for authoritative data.
    """
    if not user_message:
        return False
    text = user_message.lower()
    patterns_cfg = app_config.get("executor", {}).get("native_intent_patterns")
    # Empty list in config means "use built-in defaults" (same as unset).
    patterns = patterns_cfg if patterns_cfg else DEFAULT_NATIVE_INTENT_PATTERNS
    if any(re.search(p, text) for p in patterns):
        return True
    # Lazy import (exact copy of pre-carve behavior) keeps this module free of
    # heavy deps at import time; delegation preserves health_sleep patterns
    # (incl. custom executor.health_sleep_patterns) so wearables force native.
    from health_sleep import looks_health_sleep_query
    return looks_health_sleep_query(user_message, app_config)
