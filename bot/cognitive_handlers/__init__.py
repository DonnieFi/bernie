"""Cognitive task handler registry and dispatch helpers."""

from cognitive_handlers.registry import HANDLERS, get_handler, task_handler

__all__ = ["HANDLERS", "get_handler", "task_handler"]
