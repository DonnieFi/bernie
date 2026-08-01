"""Typed output models for cognitive workers.

Each worker that previously parsed JSON with re.search + json.loads now
returns a PydanticAI-validated instance of one of these models. See
.planning/phases/28-5-worker-output-hardening/28-5-PLAN.md for context.

Judge output models (JudgePairResult / JudgeTripletResult / TripletLegScore)
live in eval_models.py and stay there — they're judge output, not worker
output, and were shipped in feat/pydantic-ai-judges before this phase.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


# ── Reflection (bot/cognitive_workers/reflection.py) ─────────────────────────

class ReflectionNotes(BaseModel):
    """Output of ReflectionWorker — calm observational note for tomorrow morning."""
    household_summary: str = Field(max_length=300)
    # Annotated value type enforces the <=200 char rule on EACH per-person string.
    # Without Annotated, dict[str, str] only validates the value is a string.
    per_person: dict[str, Annotated[str, Field(max_length=200)]] = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)


# ── Consolidation (bot/cognitive_workers/consolidation.py) ───────────────────

class RoutineProposal(BaseModel):
    """A new routine the consolidation worker thinks it spotted."""
    name: str = Field(max_length=200)
    # KNOWN-RESIDUAL validation gap (plan-review S5, 2026-05-21): pattern is an
    # opaque dict. Define a RoutinePattern schema in a follow-up side quest once
    # production output shapes stabilize. Bad pattern blobs that pass the
    # consolidation confidence floor will still land in the routines table.
    pattern: dict = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)


class RoutineReinforcement(BaseModel):
    """Existing-routine reinforcement signal — bumps confidence on an upsert."""
    name: str = Field(max_length=200)
    confidence_bump: float = Field(ge=0, le=0.3)


class PreferenceUpdate(BaseModel):
    """A per-person preference observation (e.g. wake_time, preferred_news_topics)."""
    key: str = Field(max_length=100)
    value: str = Field(max_length=500)
    confidence: float = Field(ge=0, le=1)


class Observation(BaseModel):
    """A durable behavioural observation about a person."""
    text: str = Field(max_length=500)
    confidence: float = Field(ge=0, le=1)
    # ISO-8601 string or null. Stored as TEXT downstream; opaque pass-through.
    expires_at: str | None = None


class ConsolidationOutput(BaseModel):
    """Full output of MemoryConsolidationWorker.

    All four fields default to empty lists — an "I have nothing new to report"
    consolidation pass is valid and common.
    """
    new_routines: list[RoutineProposal] = Field(default_factory=list)
    reinforced: list[RoutineReinforcement] = Field(default_factory=list)
    preference_updates: list[PreferenceUpdate] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)


# ── Research (bot/cognitive_workers/research.py query step) ──────────────────

class EmailIngestSummary(BaseModel):
    """Typed summary for InboxIngestWorker — concise, no large quotes or secrets."""
    summary: str = Field(max_length=300)
    topics: list[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(ge=0, le=1)


class ResearchQueries(BaseModel):
    """Output of the query-generation step inside ResearchWorker.

    Cap at 3 queries matches the current _safe_json_array _coerce behaviour
    (drop everything past the third entry).
    """
    queries: list[str] = Field(max_length=3)


# ── Phase 29 Wave E — executive review on research delivery ───────────────────

class DeliverableMeta(BaseModel):
    confidence: float = Field(ge=0, le=1, default=0.75)
    urgency: Literal["low", "normal", "high"] = "normal"
    impact: Literal["low", "medium", "high"] = "medium"
    interrupt: bool = False
    draft_status: Literal["draft", "reviewed", "fallback"] = "draft"


class ResearchDeliverable(BaseModel):
    """Typed payload for research_deliver executive review + confidence routing."""
    topic: str = Field(max_length=200)
    content: str = Field(max_length=12000)
    meta: DeliverableMeta = Field(default_factory=DeliverableMeta)


# ── Phase 29 Wave F — proactive nudges ───────────────────────────────────────

class Nudge(BaseModel):
    message: str = Field(max_length=500)
    person_id: str | None = None
    confidence: float = Field(ge=0, le=1)
    source: Literal["routine", "tomorrow_context"] = "routine"
