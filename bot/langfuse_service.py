"""Langfuse tool span helper.

Wired to langfuse_logger.log_generation so every tool call produces
a traceable observation (using the existing ingestion path).
"""
from __future__ import annotations

import json
from typing import Any


async def lf_tool_span(
    *,
    tool_name: str,
    args: dict,
    result: Any,
    elapsed_ms: int,
    executor: str,
    shadow: bool,
    person_id: str | None,
) -> None:
    """Emit tool execution info to Langfuse via the shared logger.

    Uses log_generation(name=tool:xxx) so tool calls appear alongside chat
    generations. Latency is passed through. Non-fatal.
    """
    try:
        from langfuse_logger import log_generation
        inp = json.dumps(args, default=str)[:800] if isinstance(args, dict) else str(args)[:800]
        out = str(result)[:800] if result is not None else ""
        meta = {
            "tool": tool_name,
            "executor": executor,
            "shadow": bool(shadow),
        }
        if person_id:
            meta["person_id"] = person_id
        await log_generation(
            model="tool-call",
            user_input=inp,
            output=out,
            input_tokens=0,
            output_tokens=0,
            name=f"tool:{tool_name}",
            triggered_by="tool",
            metadata=meta,
            tags=["tool", tool_name],
            latency_ms=elapsed_ms,
        )
    except Exception:
        # never break the tool path
        pass
