"""MemoryConsolidationWorker — distill 7 days of activity into routines, observations.

Input sources (all per-person):
  - family_insights (last 7 days)
  - semantic_observations
  - existing routines

Outputs:
  - routines table (new + reinforced via upsert_routine)
  - semantic_observations (durable observations + preference notes)
"""
from __future__ import annotations

import json
import logging

from cognitive_workers import CognitiveWorkerBase

log = logging.getLogger("bernie.consolidation")


CONSOLIDATION_SYSTEM = (
    "You are an analytical memory consolidator for a household assistant. Given a "
    "person's recent activity and existing routines, identify recurring patterns, "
    "preference signals, and durable observations. Output STRICT JSON only — no "
    "preamble, no markdown fences. Schema:\n"
    "{\n"
    '  "new_routines":        [{"name": str, "pattern": object, "confidence": 0..1}],\n'
    '  "reinforced":          [{"name": str, "confidence_bump": 0..0.3}],\n'
    '  "preference_updates":  [{"key": str, "value": str, "confidence": 0..1}],\n'
    '  "observations":        [{"text": str, "confidence": 0..1, "expires_at": str|null}]\n'
    "}\n"
    "CRITICAL: Do NOT classify one-off events (a single concert, doctor appointment, "
    "party, test, game, or field trip) as routines. Routines must be persistent, "
    "recurring patterns with evidence of repetition. Route one-off mentions to "
    "observations with a short expires_at when worth remembering at all.\n"
    "Be conservative — only emit items you would defend with a specific message "
    "reference. Empty arrays are valid output."
)

_RECURRING_ROUTINE_MARKERS = (
    "every", "each", "weekly", "daily", "regularly", "usually", "typically", "routine",
)
_ONE_OFF_ROUTINE_MARKERS = (
    "concert", "recital", "appointment", "appointments", "dentist", "dietician", "doctor",
    "party", "parties", "birthday", "field trip", "audition",
    "game", "games", "match", "matches", "tournament", "tournaments",
    "exam", "exams", "quiz", "quizzes", "test", "tests", "interview", "wedding", "funeral",
)
_TRANSIENT_ROUTINE_MARKERS = (
    "today", "yesterday", "tomorrow", "tonight", "this morning", "this evening",
)


def _looks_like_one_off_routine(name: str, pattern: dict | None = None) -> bool:
    """Reject routine proposals that describe a dated or single occurrence."""
    text = name.lower()
    if pattern:
        text += " " + json.dumps(pattern, sort_keys=True).lower()
    if any(sig in text for sig in _TRANSIENT_ROUTINE_MARKERS):
        return True
    if any(sig in text for sig in _ONE_OFF_ROUTINE_MARKERS):
        return not any(sig in text for sig in _RECURRING_ROUTINE_MARKERS)
    return False


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _enrich_pattern_with_weekday(name: str, pattern: dict) -> dict:
    """If the routine name mentions specific weekdays and pattern has no day constraint,
    auto-populate pattern["day_of_week"] (single) or pattern["days_of_week"] (multiple)
    so the nudge engine uses the structured constraint instead of the name-parsing fallback.
    """
    if pattern.get("day_of_week") or pattern.get("days_of_week"):
        return pattern  # already set
    name_lower = name.lower()
    # Use dict.fromkeys for order-preserving dedup — plural match (d+"s") and
    # singular match (d) can both fire for the same day name e.g. "Wednesdays".
    found = list(dict.fromkeys(d for d in _WEEKDAYS if d in name_lower or (d + "s") in name_lower))
    if not found:
        return pattern
    enriched = dict(pattern)
    if len(found) == 1:
        enriched["day_of_week"] = found[0]
    else:
        enriched["days_of_week"] = found
    return enriched


class MemoryConsolidationWorker(CognitiveWorkerBase):
    name = "consolidation"

    def __init__(self, config: dict):
        cfg = config.get("cognitive_workers", {}).get("consolidation", {})
        self.default_model = cfg.get("default_model") or cfg.get("model") or "hermes3:8b-llama3.1-q6_K"
        self.upgrade_model = cfg.get("upgrade_model")
        self.escalate_above_tokens = cfg.get("escalate_above_tokens", 4000)
        self.num_ctx = cfg.get("num_ctx", 8192)
        self.max_runtime_s = cfg.get("max_runtime_s", 180)
        self.min_routine_conf = cfg.get("min_confidence_to_persist", 0.6)
        self.min_pref_conf = cfg.get("min_preference_confidence", 0.7)
        self.min_obs_conf = cfg.get("min_observation_confidence", 0.5)
        self._config = config

    async def _persist(self, person_id: str, parsed, db) -> dict:
        """Persist a validated ConsolidationOutput. Phase 28.5 §3 — `parsed` is
        now a typed ConsolidationOutput (was: a dict from json.loads).
        Pydantic already enforced 0..1 confidence ranges, so the float/try-
        except coercion that used to be here is gone. Bad rows can't slip
        in past the typed gate — they'd have already been rejected at parse
        time, not silently lowered to 0.0 and skipped."""
        counts = {"routines": 0, "preferences": 0, "observations": 0, "skipped_low_conf": 0}
        person = person_id.lower()

        for r in parsed.new_routines:
            if r.confidence < self.min_routine_conf or not r.name:
                counts["skipped_low_conf"] += 1
                continue
            if _looks_like_one_off_routine(r.name, r.pattern):
                log.info("consolidation: skipping one-off routine proposal %r for %s", r.name, person)
                counts["skipped_low_conf"] += 1
                continue
            enriched_pattern = _enrich_pattern_with_weekday(r.name, r.pattern or {})
            await db.upsert_routine(
                person_id=person,
                name=r.name[:200],
                pattern=enriched_pattern,
                confidence=r.confidence,
            )
            counts["routines"] += 1

        for r in parsed.reinforced:
            if not r.name:
                continue
            # The LLM-emitted confidence_bump becomes the +Δ in the upsert's
            # conflict clause. New-row case is rare here (reinforcement implies
            # the routine already exists) but the baseline `confidence` is kept
            # for that edge.
            await db.upsert_routine(
                person_id=person,
                name=r.name[:200],
                pattern={},
                confidence=0.5,
                reinforce_bump=r.confidence_bump,
            )
            counts["routines"] += 1

        # Route preference signals into semantic_observations with a `preference:` prefix —
        # the existing person_preferences table is structured (reminders/dm_mode/etc.)
        # and isn't a fit for freeform consolidation output.
        for p in parsed.preference_updates:
            if p.confidence < self.min_pref_conf or not p.key:
                counts["skipped_low_conf"] += 1
                continue
            txt = f"preference: {p.key} = {p.value}"
            await db.add_observation(
                person_id=person,
                observation=txt[:500],
                source="consolidation",
                confidence=p.confidence,
            )
            counts["preferences"] += 1

        for o in parsed.observations:
            if o.confidence < self.min_obs_conf or not o.text:
                counts["skipped_low_conf"] += 1
                continue
            await db.add_observation(
                person_id=person,
                observation=o.text[:500],
                source="consolidation",
                confidence=o.confidence,
                expires_at=o.expires_at,
            )
            counts["observations"] += 1

        return counts

    async def _build_prompt(self, person_id: str, db) -> str:
        person = person_id.lower()
        try:
            insights = await db.get_recent_family_insights(days=7, limit=50, person_id=person)
            ins_text = "\n".join(f"- [{i.get('source_date','')}] {i.get('insight','')}" for i in insights) or "(no recent insights)"
        except Exception:
            log.exception("consolidation: insights fetch failed")
            ins_text = "(insights unavailable)"

        try:
            obs = await db.get_observations(person_id=person, limit=30)
            obs_text = "\n".join(f"- {o.get('observation','')} (conf={o.get('confidence',0):.2f})" for o in obs) or "(no observations)"
        except Exception:
            log.exception("consolidation: observations fetch failed")
            obs_text = "(observations unavailable)"

        return (
            f"Person: {person}\n\n"
            f"Recent insights (last 7 days):\n{ins_text}\n\n"
            f"Existing observations:\n{obs_text}\n\n"
            "Produce the consolidation JSON now."
        )

    async def handle(self, task: dict, container) -> dict:
        from typed_outputs import ConsolidationOutput

        db = container.db
        payload = task.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        person_id = payload.get("person_id")
        if not person_id:
            raise ValueError("consolidation task missing person_id")

        prompt = await self._build_prompt(person_id, db)
        approx_tokens = len(prompt) // 4
        model = self.pick_model(approx_tokens)

        parsed, stats = await self.call_and_parse(
            self._config, prompt, ConsolidationOutput,
            system=CONSOLIDATION_SYSTEM,
            initial_model=model,
            num_ctx=self.num_ctx,
            timeout_s=self.max_runtime_s,
        )
        if parsed is None:
            log.warning("consolidation: validation failed after retry; emitting empty output (0 rows persisted)")
            parsed = ConsolidationOutput()

        counts = await self._persist(person_id, parsed, db)
        return {"_result": {"person_id": person_id, **counts}, "_stats": stats}
