"""Shared utilities for tool handlers in bot/tools/.

Kept dependency-light so tools/* modules don't need to import
claude_service (which pulls in the anthropic SDK).
"""
from __future__ import annotations

import re


def require_permission(group: str | None, perm_name: str, config: dict) -> tuple[bool, str]:
    """Check if a group has a specific permission. Returns (allowed, rejection_message).

    Mirrors `claude_service.require_permission` — kept here so tool handlers
    can call it without pulling in the anthropic SDK. claude_service still
    re-exports its own copy for backward compat with non-tool callers.
    """
    if group is None:
        return False, "You don't have a recognized role to do that."
    perms = config.get("permission_groups", {}).get(group, {})
    if perms.get(perm_name, False):
        return True, ""
    if group == "kids":
        return False, "That one's above my pay grade for you — ask a parent!"
    return False, "That action is admin-only."


def strip_markdown(text: str) -> str:
    """Remove markdown syntax for plain-text output channels (TTS, email body)."""
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)   # [label](url) → label
    text = re.sub(r'!\[[^\]]*\]\([^)]+\)', '', text)        # images → remove
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)            # **bold** → bold
    text = re.sub(r'\*(.+?)\*', r'\1', text)                # *italic* → italic
    text = re.sub(r'`([^`]+)`', r'\1', text)                # `code` → code
    text = re.sub(r'#{1,6}\s+', '', text)                   # # headers → text
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.M)  # --- dividers → remove
    return text.strip()


def compact_tool_result(tool_name: str, result: str) -> str:
    """Strip verbose tool results to essentials to reduce token spend."""
    if tool_name != "get_home_state":
        return result
    lines = result.splitlines()
    if len(lines) <= 2:
        return result
    compacted: list[str] = [lines[0]]
    for line in lines[1:]:
        if "Attributes:" in line:
            line = line[:line.index("Attributes:")].rstrip(" .")
        compacted.append(line)
    return "\n".join(compacted)
