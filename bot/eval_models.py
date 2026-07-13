"""Typed output models for eval_service judges.

Used by judge_pair and judge_triplet via PydanticAI structured output.
Placing models here keeps eval_service.py focused on orchestration and
lets future workers import shared schemas without circular imports.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class JudgePairResult(BaseModel):
    """Output schema for judge_pair: 4 float scores in [0, 1]."""
    a_intent: float = Field(ge=0.0, le=1.0, description="Response A intent match score")
    a_factual: float = Field(ge=0.0, le=1.0, description="Response A factual grounding score")
    b_intent: float = Field(ge=0.0, le=1.0, description="Response B intent match score")
    b_factual: float = Field(ge=0.0, le=1.0, description="Response B factual grounding score")


class TripletLegScore(BaseModel):
    """Per-leg scores for judge_triplet: 3 ints in [0, 10]."""
    intent_match: int = Field(ge=0, le=10)
    tool_accuracy: int = Field(ge=0, le=10)
    preference: int = Field(ge=0, le=10)


class JudgeTripletResult(BaseModel):
    """Output schema for judge_triplet: winner + per-leg scores + reasoning."""
    winner: Literal["A", "B", "C", "none"]
    reasoning: str
    A: TripletLegScore
    B: TripletLegScore
    C: TripletLegScore
