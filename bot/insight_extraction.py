"""Pure helpers for nightly digest insight extraction (testable without LLM deps)."""
from __future__ import annotations

import re

DIGEST_SYSTEM = (
    "You are an assistant extracting behavioral insights from conversation logs. "
    "Extract only recurring preferences, habits, or stable facts — not one-off calendar "
    "events (concerts, appointments, tests, parties, games). "
    "CRITICAL: Do not use relative time words like 'today', 'tomorrow', or 'tonight'; "
    "use absolute days of the week or omit the date entirely."
)

_RECURRING_SIGNALS = (
    "always", "never", "prefers", "hates", "loves", "routine",
    "every day", "every morning", "every week", "every tuesday", "every thursday",
    "regularly", "usually", "typically", "each week",
)
_TRANSIENT_SIGNALS = (
    "today", "yesterday", "tomorrow", "tonight", "this morning", "this evening",
    "this afternoon", "this week", "next week",
)
_ONE_OFF_SIGNALS = (
    "concert", "recital", "appointment", "appointments", "dentist", "dietician",
    "doctor visit", "birthday party", "field trip", "audition",
    "game", "games", "match", "matches", "tournament", "tournaments",
    "exam", "exams", "quiz", "quizzes", "test", "tests", "interview",
    "party", "parties",
)


def looks_like_one_off_insight(text: str) -> bool:
    """Drop dated one-off events that slipped past the extraction prompt."""
    lower = text.lower()
    if any(sig in lower for sig in _TRANSIENT_SIGNALS):
        return True
    if any(sig in lower for sig in _ONE_OFF_SIGNALS):
        return not any(sig in lower for sig in _RECURRING_SIGNALS)
    return False


def parse_insights_from_response(text: str) -> list[dict]:
    """Parse model output into insight dicts for family_insights storage."""
    insights = []
    for line in text.splitlines():
        line = line.strip()
        for prefix in ("- ", "* ", "• "):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        line = re.sub(r"^\d+[\.\)]\s+", "", line)
        if not line or len(line) < 10:
            continue
        if looks_like_one_off_insight(line):
            continue

        lower_line = line.lower()
        is_perm = any(sig in lower_line for sig in _RECURRING_SIGNALS)

        insights.append({
            "text": line,
            "is_permanent": is_perm,
            "expires_days": 14,
        })
    return insights


def build_digest_user_prompt(person_name: str, conversation: str, *, source: str = "yesterday") -> str:
    """Shared user prompt for insight extraction across primary and fallback paths."""
    return (
        f"Analyze the following conversation from {source} and extract 2-3 insights "
        f"about {person_name} that Bernie should remember.\n"
        f"Return insights as a simple list, one per line. Each insight should be one clear sentence.\n"
        f"Only include recurring preferences, habits, or stable facts — NOT one-off events "
        f"(concerts, appointments, tests, parties, games).\n"
        f"Do not use relative time words like 'today', 'tomorrow', or 'tonight'; "
        f"use absolute days of the week or omit the date.\n"
        f"Examples:\n"
        f"- Dad usually checks his calendar every morning\n"
        f"- Mom prefers reminders 30 minutes early\n"
        f"- Child1 is busy with school on Tuesdays and Thursdays\n\n"
        f"Be specific. Only include things you're confident about based on the conversation. "
        f"If the conversation doesn't reveal clear patterns for {person_name}, return fewer insights or none.\n\n"
        f"Conversation:\n{conversation}"
    )
