"""ReflectionWorker — nightly observational summary feeding the 7am /summary.

Inputs: today's family_insights + tomorrow's calendar events + recent observations.
Output: tomorrow_context rows (one household + one per family member with content).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from cognitive_workers import CognitiveWorkerBase

log = logging.getLogger("bernie.reflection")


REFLECTION_SYSTEM = (
    "You are an observational household assistant. Given today's family activity and "
    "tomorrow's schedule, write a calm, observational note for tomorrow morning. "
    "Tone: calm, observational, never prescriptive. Output STRICT JSON only — no "
    "preamble, no markdown fences. Keys: household_summary (string, <=300 chars), "
    "per_person (object mapping lowercase person name to <=200 char string), "
    "confidence (number 0..1).\n"
    "CRITICAL: Tomorrow's calendar is the source of truth for dated events — mention "
    "them in per_person notes when relevant, but do not describe them as ongoing "
    "routines or permanent facts. Ignore stale insights about one-off activities "
    "when they conflict with the calendar. Use absolute day/date references, not "
    "'today', 'tomorrow', or 'tonight'."
)


class ReflectionWorker(CognitiveWorkerBase):
    name = "reflection"

    def __init__(self, config: dict, cal_service=None):
        cfg = config.get("cognitive_workers", {}).get("reflection", {})
        self.default_model = cfg.get("default_model") or cfg.get("model") or "hermes3:8b-llama3.1-q6_K"
        self.upgrade_model = cfg.get("upgrade_model")
        self.escalate_above_tokens = cfg.get("escalate_above_tokens", 4000)
        self.num_ctx = cfg.get("num_ctx", 8192)
        self.max_runtime_s = cfg.get("max_runtime_s", 120)
        self._config = config
        self._cal = cal_service

    async def _build_prompt(self, for_date: str, db) -> str:
        # Tomorrow's calendar
        cal_text = "(calendar unavailable)"
        if self._cal is not None:
            try:
                from zoneinfo import ZoneInfo
                _tz = ZoneInfo(self._config.get("timezone", "America/Halifax"))
                start = datetime.fromisoformat(for_date).replace(tzinfo=_tz)
                events = await self._cal.get_events_starting(start, 1)
                if events:
                    cal_text = "\n".join(
                        f"- {e.get('summary','?')} @ {e.get('start','?')}" for e in events[:30]
                    )
                else:
                    cal_text = "(no events tomorrow)"
            except Exception:
                log.exception("reflection: cal_service.get_events_starting failed")
        # Today's insights (per family_insights, written nightly by nightly_digest)
        try:
            insights = await db.get_recent_family_insights(days=1, limit=20)
            ins_text = "\n".join(
                f"- [{i.get('person_id','?')}] {i.get('insight','')}" for i in insights
            ) or "(no recent insights)"
        except Exception:
            log.exception("reflection: get_recent_family_insights failed")
            ins_text = "(insights unavailable)"
        return (
            f"Tomorrow is {for_date}.\n\nTomorrow's calendar:\n{cal_text}\n\n"
            f"Today's insights:\n{ins_text}\n\n"
            "Write the reflection JSON now."
        )

    async def handle(self, task: dict, container) -> dict:
        db = container.db
        from typed_outputs import ReflectionNotes

        payload = task.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        for_date = payload.get("for_date")
        if not for_date:
            raise ValueError("reflection task missing for_date")

        prompt = await self._build_prompt(for_date, db)
        approx_tokens = len(prompt) // 4
        model = self.pick_model(approx_tokens)

        notes, stats = await self.call_and_parse(
            self._config, prompt, ReflectionNotes,
            system=REFLECTION_SYSTEM,
            initial_model=model,
            num_ctx=self.num_ctx,
            timeout_s=self.max_runtime_s,
        )
        if notes is None:
            log.warning("reflection: validation failed after retry; emitting empty record (confidence=0)")
            notes = ReflectionNotes(household_summary="", per_person={}, confidence=0.0)

        household = notes.household_summary.strip()
        per_person = notes.per_person
        confidence = notes.confidence

        rows = 0
        if household:
            await db.upsert_tomorrow_context(
                for_date=for_date,
                summary=household[:500],
                person_id=None,
                confidence=confidence,
                source_task_id=task.get("id"),
            )
            rows += 1
        # Lowercase first, then dedup. The LLM may emit both "Dad" and
        # "dad" — without this merge whichever key the model happened to
        # emit second wins, and the model's key-emission order is not a
        # signal we want deciding what gets persisted.
        normalized: dict[str, str] = {}
        for name, summary in per_person.items():
            if not summary:
                continue
            key = str(name).lower()
            normalized[key] = str(summary)
        for person_id_lower, summary in normalized.items():
            await db.upsert_tomorrow_context(
                for_date=for_date,
                summary=summary[:500],
                person_id=person_id_lower,
                confidence=confidence,
                source_task_id=task.get("id"),
            )
            rows += 1

        return {"_result": {"rows": rows, "for_date": for_date}, "_stats": stats}
