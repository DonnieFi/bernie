"""Per-discord-user queue of research tasks awaiting an emoji delivery-choice.

When `request_research` enqueues a task, it pushes (task_id, default_delivery)
keyed by the requester's discord snowflake. The Discord bot drains this queue
right after sending Bernie's reply, reacts 💬/✉️ on the just-sent message,
and stores a `message_event_map` row so `on_reaction_add` can find the task.

Entries time out after `_TTL_SECONDS` to prevent leaks when a research call
isn't followed by a Discord message (e.g., API-only calls).
"""
from __future__ import annotations

import time
from collections import defaultdict

_TTL_SECONDS = 300  # 5-minute timeout

_pending: dict[str, list[dict]] = defaultdict(list)


def register(discord_id: str, task_id: int, topic: str, default_delivery: str = "dm") -> None:
    """Record a research task that wants a delivery-choice prompt."""
    if not discord_id:
        return
    _pending[str(discord_id)].append({
        "task_id": int(task_id),
        "topic": topic,
        "default": default_delivery,
        "ts": time.time(),
    })


def drain(discord_id: str) -> list[dict]:
    """Return all non-stale pending entries for a user and clear them."""
    if not discord_id:
        return []
    key = str(discord_id)
    entries = _pending.pop(key, [])
    now = time.time()
    return [e for e in entries if (now - e["ts"]) <= _TTL_SECONDS]


def prune_stale() -> int:
    """Drop entries older than the TTL. Returns count removed."""
    now = time.time()
    removed = 0
    for key in list(_pending.keys()):
        kept = [e for e in _pending[key] if (now - e["ts"]) <= _TTL_SECONDS]
        removed += len(_pending[key]) - len(kept)
        if kept:
            _pending[key] = kept
        else:
            del _pending[key]
    return removed
