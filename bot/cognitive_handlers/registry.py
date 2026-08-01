"""Task-type → handler registry for CognitiveWorker."""

from __future__ import annotations

from typing import Awaitable, Callable

HANDLERS: dict[str, Callable[..., Awaitable]] = {}


def task_handler(task_type: str):
    """Decorator to register an async handler for a cognitive task type."""

    def decorator(fn: Callable[..., Awaitable]):
        HANDLERS[task_type] = fn
        return fn

    return decorator


def get_handler(task_type: str) -> Callable[..., Awaitable] | None:
    return HANDLERS.get(task_type)
