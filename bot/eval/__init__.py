"""Eval package (Phase 4.3 complete).

Modules: _http, shadow, judges, hitl, nightly, audit, reporting.
`eval_service.py` is a thin facade that re-exports this public API for backwards compat.
"""
from .shadow import fire_shadow_call, fire_shadow_triplet
from .policy import resolve_eval_policy
from .judges import judge_pair, judge_triplet
from .hitl import send_hitl_dms, handle_hitl_reaction
from .nightly import nightly_eval_worker, build_nightly_summary

from .audit import audit_ungrounded_live_data, format_ungrounded_audit_section
from .reporting import build_weekly_cognitive_summary, weekly_cognitive_report_worker

__all__ = [
    "fire_shadow_call",
    "fire_shadow_triplet",
    "judge_pair",
    "judge_triplet",
    "send_hitl_dms",
    "handle_hitl_reaction",
    "nightly_eval_worker",
    "build_nightly_summary",
    "audit_ungrounded_live_data",
    "format_ungrounded_audit_section",
    "build_weekly_cognitive_summary",
    "weekly_cognitive_report_worker",
    "resolve_eval_policy",
]
