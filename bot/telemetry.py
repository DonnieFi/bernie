"""Telemetry helpers — safe background logging and stats building.

Used by chat paths, cognitive workers (BTS), shadow evaluation, and nightly tasks
so that token usage, Langfuse generations, and cost data are never silently dropped.
"""

from __future__ import annotations

import asyncio
from typing import Any

log = __import__("logging").getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()


def fire_and_forget(coro) -> asyncio.Task:
    """Run an async coroutine in the background with a strong reference.

    Prevents the GC from dropping fire-and-forget logging tasks (Langfuse + DB)
    before they complete their network I/O.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def build_stats(
    *,
    model: str | None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    duration_ms: int | None = None,
    gpu_ms: int | None = None,
    **extra: Any,
) -> dict:
    """Standard stats shape returned by workers and executors.

    Guarantees the fields expected by CognitiveWorker, eval_service, and
    db.log_token_usage.
    """
    return {
        "model": model or "unknown",
        "tokens_in": int(tokens_in or 0),
        "tokens_out": int(tokens_out or 0),
        "cache_creation_tokens": int(cache_creation_tokens or 0),
        "cache_read_tokens": int(cache_read_tokens or 0),
        "duration_ms": duration_ms,
        "gpu_ms": gpu_ms,
        **extra,
    }
