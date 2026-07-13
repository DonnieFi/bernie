"""Thin facade — re-exports only. Production code should import from llm/* directly.

Retained for test patches and gradual caller migration.
"""
from __future__ import annotations

from telemetry import fire_and_forget  # compat re-export for legacy tests/callers
from llm.runtime import get_db as _get_db, get_container

# Compat: tests patch claude_service._container directly.
_container = None


def _init_runtime(container) -> None:
    """Wire ServiceContainer — delegates to llm.runtime and mirrors _container."""
    global _container
    from llm.runtime import init as _runtime_init

    _runtime_init(container)
    _container = container


# Public name used by main.py and tests
_init = _init_runtime

# Constants — canonical homes elsewhere; re-exported for transitional imports.
from constants import ROLE_ADMIN, ROLE_ALL, ROLE_BERNIE, ROLE_PARENTS, ROLE_SYSTEM
from model_registry import DEFAULT_MODEL

# LLM package re-exports (facade compat)
from llm.intent import looks_live_data as _looks_live_data, looks_multistep as _looks_multistep
from llm.routing import get_executor as _get_executor
from llm.pipeline import run_loop as _run_loop
from llm.messages import (
    _HISTORY_VERBATIM_TAIL,
    _TOOL_RESULT_STUB,
    extract_last_user_message as _extract_last_user_message,
    prune_old_tool_results as _prune_old_tool_results,
    prepare_messages as _prepare_messages,
)
from llm.observability import log_llm_turn as _lf_log_generation
from llm.clients import (
    make_client as _make_client,
    make_observed_anthropic_client,
    model_cache_support,
    llm_client_is_ephemeral as _llm_client_is_ephemeral,
    close_client as _close_client,
)
from llm.hashing import hashable_system_prefix as _hashable_system_prefix
from llm.context_builder import build_context
from llm.ollama import resolve_ollama_target as _resolve_ollama_target, call_ollama as _call_ollama
from llm.model_state import set_model, get_model_info, _base_url_for_model
from llm.shadow_hooks import maybe_fire_shadow as _maybe_fire_shadow
from llm.services import build_service_refs as _build_service_refs
from llm.chat import chat_general, chat, chat_meal_planning
from llm.audit import AUDIT_SYSTEM_PROMPT, call_for_audit
from llm.compat import execute_tool as _execute_tool, find_task as _find_task
