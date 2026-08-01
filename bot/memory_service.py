# bot/memory_service.py

import logging
from datetime import datetime, timezone, timedelta
from db_binding import get_database
import db_writes

log = logging.getLogger(__name__)


async def record_acknowledged(person_id: str, event_title: str) -> None:
    """Call this when someone RSVPs ✅ to a reminder."""
    await db_writes.routed("insert_memory_event", person_id, "acknowledged", event_title)


async def record_missed(person_id: str, event_title: str) -> None:
    """Call this when a reminder gets no RSVP within X minutes."""
    await db_writes.routed("insert_memory_event", person_id, "missed", event_title)


async def get_patterns(person_id: str) -> list[dict]:
    """Returns a list of patterns based on memory events."""
    return await get_database().get_memory_event_patterns(person_id)


async def get_memory_context(person_id: str) -> str:
    """
    Returns a short memory context string to inject into Claude prompts.
    Combines RSVP behavioural patterns with generated insights from family_insights.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    missed_rows, ack_rows = await get_database().get_memory_behavior_since(person_id, since)

    memory_text = ""
    if missed_rows or ack_rows:
        parts = []
        if missed_rows:
            missed_list = [f"{r['event_title']} ({r['count']}x)" for r in missed_rows]
            parts.append(f"recently missed reminders for: {', '.join(missed_list)}")
        if ack_rows:
            ack_rows_sorted = sorted(ack_rows, key=lambda x: x["count"], reverse=True)
            ack_list = [f"{r['event_title']}" for r in ack_rows_sorted[:3]]
            parts.append(f"consistently acknowledges: {', '.join(ack_list)}")
        memory_text = f"Bernie's memory of {person_id}: They have " + " and ".join(parts) + "."

    insights = await get_database().get_active_insights(person_id)
    if insights:
        insights_block = "\n\nGenerated insights:\n" + "\n".join(f"  • {i}" for i in insights)
        memory_text += insights_block

    return memory_text


async def prune_old_events() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    return await db_writes.routed("prune_memory_events_before", cutoff)
