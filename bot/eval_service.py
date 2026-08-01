"""
Eval thin facade (Phase 4.3 complete).

All implementation lives under `bot/eval/`:
  _http.py, shadow.py, judges.py, hitl.py, nightly.py, audit.py, reporting.py

This module re-exports the same public names (and test-patched privates) so that
`from eval_service import ...` and `patch("eval_service....")` keep working with
zero caller changes.

Do not add business logic here — only imports and re-exports.
"""
# Re-exported for `from eval_service import ...` (not used in-module; noqa for F401)
from eval._http import (  # noqa: F401
    ANTHROPIC_KEY,
    DEFAULT_JUDGE_MODEL,
    _LF_HOST,
    _LF_PUBLIC,
    _LF_SECRET,
    _session_or_new,
    _ssl_for,
)

from eval.policy import resolve_eval_policy, harness_active  # noqa: F401

from eval.shadow import (  # noqa: F401
    fire_shadow_call,
    fire_shadow_triplet,
    _call_model_shadow,
    _call_harness_shadow,
    _call_shadow_model,
    _call_litellm_shadow,
    _call_ollama_shadow,
    _build_shadow_messages,
    _log_to_langfuse,
)

from eval.judges import (  # noqa: F401
    judge_pair,
    judge_triplet,
    _make_judge_agent,
    _judge_fallback_chain,
    _run_judge_with_fallbacks,
    _is_transient_judge_error,
)

from eval.hitl import send_hitl_dms, handle_hitl_reaction  # noqa: F401

from eval.nightly import (  # noqa: F401
    nightly_eval_worker,
    build_nightly_summary,
    _log_triplet_scores_to_langfuse,
)

from eval.audit import (  # noqa: F401
    _grounding_tool_names,
    _TOOL_NAME_RE,
    _NUMERIC_CLAIM_RE,
    _AUDIT_WINDOW_MINUTES,
    _parse_audit_ts,
    _parse_tool_name,
    _response_has_numeric_claim,
    _pair_user_assistant_turns,
    _tool_call_near_turn,
    _fetch_conversation_since,
    _fetch_tool_calls_since,
    audit_ungrounded_live_data,
    format_ungrounded_audit_section,
)

from eval.reporting import (  # noqa: F401
    build_weekly_cognitive_summary,
    weekly_cognitive_report_worker,
)
