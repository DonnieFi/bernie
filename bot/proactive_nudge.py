"""Proactive nudge scan — routines + tomorrow_context (Phase 29 Wave F)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from typed_outputs import Nudge
import db_writes

log = logging.getLogger(__name__)

_MORNING_ROUTINE_HOURS = range(6, 11)
_MORNING_CONTEXT_HOURS = range(7, 10)
# Tighter than bare final|project|presentation (review feedback)
_DEFAULT_STUDY_KEYWORDS = (
    r"test|exam|quiz|rehearsal|recital|audition|midterm|finals?|final exam"
)


def _tz(config: dict):
    return ZoneInfo(config.get("timezone", "America/Halifax"))


def _study_pattern(config: dict) -> re.Pattern:
    raw = (
        config.get("cognitive_workers", {}).get("study_keywords")
        or _DEFAULT_STUDY_KEYWORDS
    )
    return re.compile(raw, re.IGNORECASE)


_WEEKDAYS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
)
_PERSON_FACT_MARKERS = (
    "likely bernie",
    "preference",
    "primary mode",
    "car preference",
    "works at",
    "partner or spouse",
    "drives familycar",
)


def _parse_pattern_json(row: dict) -> dict:
    pj = row.get("pattern_json")
    if not pj:
        return {}
    if isinstance(pj, dict):
        return pj
    if isinstance(pj, str):
        try:
            parsed = json.loads(pj)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _name_looks_like_person_fact(name: str, person_id: str | None) -> bool:
    """Semantic observations (not timed habits) should not become morning DMs."""
    if not person_id:
        return False
    pid = person_id.lower()
    n = (name or "").strip().lower()
    if not n:
        return True
    if n == pid:
        return True
    return any(marker in n for marker in _PERSON_FACT_MARKERS)


def _routine_has_temporal_signal(row: dict) -> bool:
    """True when the routine encodes a schedule, not just a consolidation fact."""
    pattern = _parse_pattern_json(row)
    if any(
        pattern.get(k)
        for k in ("day_of_week", "days_of_week", "start_time", "time", "time_of_day")
    ):
        return True
    name = (row.get("name") or "").strip()
    name_lower = name.lower()
    if any(d in name_lower or f"{d}s" in name_lower for d in _WEEKDAYS):
        return True
    # No schedule metadata: allow short habit names ("Pack lunch"); block
    # person-prefixed consolidation rows ("Mom likes X") that would DM spam.
    person_id = row.get("person_id")
    if person_id:
        pid = str(person_id).lower()
        if (
            name_lower == pid
            or name_lower.startswith(pid + " ")
            or name_lower.startswith(pid + "'")
        ):
            return False
    if _name_looks_like_person_fact(name, person_id):
        return False
    return True


def collect_nudge_candidates(
    *,
    config: dict,
    routines: list[dict],
    tomorrow_rows: list[dict],
    now: datetime | None = None,
) -> list[Nudge]:
    """Rule-based nudge candidates (no LLM)."""
    tz = _tz(config)
    now = now or datetime.now(tz)
    hour = now.hour
    nudges: list[Nudge] = []

    if hour in _MORNING_CONTEXT_HOURS:
        study_re = _study_pattern(config)
        for row in tomorrow_rows:
            summary = (row.get("summary") or "").strip()
            if not summary:
                continue
            if study_re.search(summary):
                nudges.append(
                    Nudge(
                        message=summary[:500],
                        person_id=row.get("person_id") or None,
                        confidence=float(row.get("confidence") or 0.65),
                        source="tomorrow_context",
                    )
                )

    if hour in _MORNING_ROUTINE_HOURS:
        current_day = now.strftime("%A").lower()

        for row in routines:
            conf = float(row.get("confidence") or 0)
            if conf < 0.6:
                continue

            person_id = row.get("person_id")
            name = (row.get("name") or "routine").strip()
            if _name_looks_like_person_fact(name, person_id):
                continue
            if not _routine_has_temporal_signal(row):
                continue

            # Check day of week constraints in pattern_json
            pattern = _parse_pattern_json(row)

            # day_of_week constraint
            day_constraint = pattern.get("day_of_week")
            if day_constraint and isinstance(day_constraint, str):
                if day_constraint.lower() != current_day:
                    continue

            # days_of_week constraint
            days_constraint = pattern.get("days_of_week")
            if days_constraint and isinstance(days_constraint, list):
                days_lower = [d.lower() for d in days_constraint if isinstance(d, str)]
                if current_day not in days_lower:
                    continue

            # Parse weekday name from routine description / name
            name_lower = name.lower()
            mentioned_days = [d for d in _WEEKDAYS if d in name_lower or (d + "s") in name_lower]
            if mentioned_days and current_day not in mentioned_days:
                continue

            nudges.append(
                Nudge(
                    message=f"Heads up — `{name}` is usually around this time.",
                    person_id=person_id,
                    confidence=conf,
                    source="routine",
                )
            )

    # Cap, dedupe by message prefix, and max one routine nudge per person
    seen: set[str] = set()
    routine_persons: set[str] = set()
    out: list[Nudge] = []
    for n in nudges:
        key = (n.person_id or "household", n.message[:80])
        if key in seen:
            continue
        if n.source == "routine" and n.person_id:
            if n.person_id in routine_persons:
                continue
            routine_persons.add(n.person_id)
        seen.add(key)
        out.append(n)
        if len(out) >= 3:
            break
    return out


async def _nudge_already_sent_or_muted(nudge: Nudge, db) -> bool:
    try:
        from datetime import datetime, timedelta, timezone
        since_dt = datetime.now(timezone.utc) - timedelta(hours=12)
        since_iso = since_dt.isoformat()
        routine_daily_since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        # We check sent, acknowledged, or dismissed nudges
        for etype in ["proactive_nudge", "proactive_nudge_ack", "proactive_nudge_dismiss"]:
            rows = await db.fetch_activity_since(etype, since_iso)
            for r in rows:
                desc = r.get("description") or ""
                # Match by prefix/description
                if desc.startswith(nudge.message[:180]) or nudge.message.startswith(desc[:180]):
                    pid = r.get("person_id")
                    if pid == nudge.person_id:
                        return True

        # At most one routine nudge per person per day (stops consolidation junk spam).
        if nudge.source == "routine" and nudge.person_id:
            rows = await db.fetch_activity_since("proactive_nudge", routine_daily_since)
            for r in rows:
                if r.get("person_id") == nudge.person_id:
                    return True
    except Exception:
        log.exception("Failed to check if nudge was already sent/muted")
    return False


async def _deliver_nudge(nudge: Nudge, config: dict, router) -> bool:
    if router is None:
        return False
    recipient_id = None
    if nudge.person_id:
        try:
            from task_access import person_to_discord_id

            did = person_to_discord_id(nudge.person_id)
            if did:
                recipient_id = str(did)
        except Exception:
            log.debug("proactive_nudge: person_to_discord_id failed", exc_info=True)
    if not recipient_id:
        recipient_id = str(config.get("schedule_channel_id") or "")
    if not recipient_id:
        return False
    try:
        event_id = f"nudge:{nudge.person_id or ''}:{nudge.message[:80]}"
        results = await router.notify(
            router.notification(
                recipient_id=recipient_id,
                message=nudge.message,
                urgency="normal",
                event_id=event_id,
                message_type="proactive_nudge",
            )
        )
        # Add reactions and message mapping if successfully sent to Discord
        msg = results.get("discord")
        if msg and hasattr(msg, "id"):
            try:
                await msg.add_reaction("✅")
                await msg.add_reaction("❌")
                from db_binding import get_database
                db = get_database()
                # Store message mapping to link the Discord message to the nudge action
                await db_writes.routed("store_message_mapping", 
                    message_id=msg.id,
                    event_id=event_id,
                    message_type="proactive_nudge"
                )
            except Exception:
                log.debug("proactive_nudge: failed to add reactions / store mapping", exc_info=True)

        return bool(results.get("discord") or results.get("status") == "queued_quiet_hours")
    except Exception:
        log.exception("proactive_nudge: delivery failed for %s", recipient_id)
        return False


async def run_proactive_nudge_scan(config, db, router) -> int:
    """Scan routines + tomorrow_context; deliver up to 3 nudges. Returns count sent."""
    tz = _tz(config)
    today = datetime.now(tz).date().isoformat()
    routines = await db.get_routines(min_confidence=0.55)
    tomorrow_rows: list[dict] = []
    household = await db.get_tomorrow_context(today, person_id=None)
    if household:
        tomorrow_rows.append(household)
    for row in routines:
        pid = row.get("person_id")
        if not pid:
            continue
        ctx = await db.get_tomorrow_context(today, person_id=pid)
        if ctx:
            tomorrow_rows.append(ctx)

    candidates = collect_nudge_candidates(
        config=config,
        routines=routines,
        tomorrow_rows=tomorrow_rows,
    )
    
    # Filter out candidates that were already sent or muted in the last 12 hours
    filtered_candidates = []
    for nudge in candidates:
        if await _nudge_already_sent_or_muted(nudge, db):
            continue
        filtered_candidates.append(nudge)

    sent = 0
    for nudge in filtered_candidates:
        if await _deliver_nudge(nudge, config, router):
            sent += 1
            try:
                await db_writes.routed("log_activity", 
                    event_type="proactive_nudge",
                    description=nudge.message[:200],
                    person_id=nudge.person_id,
                    meta=json.dumps({"source": nudge.source, "confidence": nudge.confidence}),
                )
            except Exception:
                log.debug("proactive_nudge: activity log failed", exc_info=True)
    if sent:
        log.info("proactive_nudge: delivered %d nudge(s)", sent)
    return sent
