"""Prompt hash helpers shared by chat and eval paths."""

from __future__ import annotations


def hashable_system_prefix(system, n: int = 200) -> str:
    """Stable system prompt prefix for prompt_hash deduplication."""
    if isinstance(system, str):
        return system[:n]
    if isinstance(system, list):
        return "".join(b.get("text", "") for b in system if isinstance(b, dict))[:n]
    return ""
