"""Route executive-reviewed deliverables by confidence / urgency / impact."""

from __future__ import annotations

from typing import Literal

from typed_outputs import DeliverableMeta

RouteAction = Literal["ignore", "remember", "suggest", "interrupt"]

_DEFAULT_THRESHOLDS = {
    "ignore_below": 0.35,
    "suggest_above": 0.55,
    "interrupt_confidence": 0.65,
}


def _thresholds(config: dict | None) -> dict:
    cfg = (config or {}).get("executive_review") or {}
    return {
        "ignore_below": float(cfg.get("ignore_below", _DEFAULT_THRESHOLDS["ignore_below"])),
        "suggest_above": float(cfg.get("suggest_above", _DEFAULT_THRESHOLDS["suggest_above"])),
        "interrupt_confidence": float(
            cfg.get("interrupt_confidence", _DEFAULT_THRESHOLDS["interrupt_confidence"])
        ),
    }


def route_deliverable(meta: DeliverableMeta, *, config: dict | None = None) -> RouteAction:
    """Choose delivery path for a reviewed deliverable."""
    t = _thresholds(config)
    if meta.confidence < t["ignore_below"]:
        return "ignore"
    if meta.interrupt and meta.urgency == "high" and meta.confidence >= t["interrupt_confidence"]:
        return "interrupt"
    if meta.impact == "high" and meta.confidence >= t["suggest_above"]:
        return "suggest"
    if meta.confidence >= t["suggest_above"]:
        return "suggest"
    return "remember"
