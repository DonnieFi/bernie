"""bot/llm package (Phase 4.4 LLMPipeline carve).

Thin package for intent, routing, pipeline, clients, observability, etc.
claude_service.py remains the thin facade that re-exports for backwards compat.
"""
from .intent import looks_live_data, looks_multistep
from .hashing import hashable_system_prefix
from .observability import log_llm_turn
from .clients import (
    make_client,
    make_observed_anthropic_client,
    llm_client_is_ephemeral,
    close_client,
    model_cache_support,
)
from .messages import (
    extract_last_user_message,
    prune_old_tool_results,
    prepare_messages,
)
from .routing import get_executor
from .pipeline import run_loop
from .ollama import resolve_ollama_target, call_ollama
from .audit import call_for_audit
from .model_state import set_model, get_model_info, _base_url_for_model, DEFAULT_MODEL
from .services import build_service_refs
from .shadow_hooks import maybe_fire_shadow
from .chat import chat_general, chat, chat_meal_planning
from .turn_timer import TurnTimer

__all__ = [
    "looks_live_data",
    "looks_multistep",
    "hashable_system_prefix",
    "log_llm_turn",
    "make_client",
    "make_observed_anthropic_client",
    "llm_client_is_ephemeral",
    "close_client",
    "model_cache_support",
    "extract_last_user_message",
    "prune_old_tool_results",
    "prepare_messages",
    "get_executor",
    "run_loop",
    "resolve_ollama_target",
    "call_ollama",
    "call_for_audit",
    "set_model",
    "get_model_info",
    "_base_url_for_model",
    "DEFAULT_MODEL",
    "build_service_refs",
    "maybe_fire_shadow",
    "chat_general",
    "chat",
    "chat_meal_planning",
    "TurnTimer",
]
