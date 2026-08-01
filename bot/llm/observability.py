"""LLM observability helpers (Phase 4.4 Session 1).

Every LLM completion — chat, audit, Ollama fallback, shadow — logs two things:
  1. database.log_token_usage  (billing / dashboard source of truth)
  2. langfuse_logger.log_generation  (tracing / observability)

This module provides `log_llm_turn()` as the single entrypoint so no caller
has to remember both. `_lf_log_generation` is a compat alias; native.py imports
`log_llm_turn` from here directly (facade re-exports for legacy callers).

Dependencies: db_binding (for get_database), langfuse_logger, telemetry.
Does NOT import claude_service — callers pass db_module or we fall back to
db_binding.get_database().
"""
from __future__ import annotations

import logging
from typing import Any
import db_writes

log = logging.getLogger(__name__)


async def log_llm_turn(
    *,
    model: str,
    user_input: str,
    output: str,
    input_tokens: int,
    output_tokens: int,
    triggered_by: str = "discord",
    actor_id: str = "",
    session_id: str | None = None,
    conversation_id: str | None = None,
    mode: str | None = None,
    name: str = "chat",
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    latency_ms: int | None = None,
    cost_usd: float | None = None,
    db_module: Any = None,
    surface: str = "discord",
    # Wave 2 surface observability (Langfuse tags)
    tools_advertised: int | None = None,
    tool_domain_count: int | None = None,
) -> None:
    """Log a chat generation to both the local SQLite DB and Langfuse.

    Parameters match the union of fields required by `database.log_token_usage`
    and `langfuse_logger.log_generation`. Callers should prefer the keyword-only
    interface for clarity.
    """
    # 1. Local DB (source of truth for billing/dashboards)
    try:
        db = db_module
        if db is None:
            from db_binding import get_database
            db = get_database()
        await db_writes.routed("log_token_usage", 
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            conversation_id=conversation_id,
            triggered_by=triggered_by,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            session_id=session_id,
            surface=surface,
        )
    except Exception:
        log.exception("Failed to log token usage to local DB")

    # 2. Langfuse (observability/tracing)
    try:
        from langfuse_logger import log_generation
        lf_meta: dict[str, str] = {"source": "direct_anthropic"}
        lf_tags: list[str] = ["direct_anthropic"]
        if mode:
            lf_meta["mode"] = mode
            lf_tags.append(f"mode:{mode}")
        if tools_advertised is not None:
            lf_meta["tools_advertised"] = str(tools_advertised)
            lf_tags.append(f"tools_advertised:{tools_advertised}")
        if tool_domain_count is not None:
            lf_meta["tool_domain_count"] = str(tool_domain_count)
            lf_tags.append(f"tool_domains:{tool_domain_count}")

        await log_generation(
            model=model,
            user_input=user_input,
            output=output,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            name=name,
            actor_id=actor_id,
            triggered_by=triggered_by,
            session_id=session_id,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            latency_ms=latency_ms,
            metadata=lf_meta,
            tags=lf_tags,
            cost_usd=cost_usd,
        )
    except Exception:
        log.debug("Langfuse trace failed (non-fatal)", exc_info=True)
