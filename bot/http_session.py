"""40B-1e: shared aiohttp.ClientSession accessor.

Route outbound HTTP through ServiceContainer.session (created in main.py).
Do not close the returned session — it is process-lifetime.

Callers must pass per-request headers (Authorization, x-api-key, etc.) on
individual get/post calls — never mutate session-level defaults on the shared
session or auth will bleed across unrelated backends.

Runtime: api/cognition use ServiceContainer.session only (no bot.get_session).
discord/monolith/tests may fall back to bot.get_session() when the container
session is missing (family-bot-5vw).

Documented exceptions (may still construct ad-hoc ClientSession):
- db_client.py — cognition RPC session (dedicated lifecycle)
- watchman.py — Docker UnixConnector sessions
- ha_service.py, frigate_service.py, network_service.py — owned service sessions
- transit_service.py — optional owned session when session=None (per-request timeout on feed get)
- ollama_resolver.py — ephemeral probe when no session passed (startup/tests)
- inspect_tomorrow*.py, api_tester.py, manual scripts — dev utilities
- tests — mocks patch get_http_session or aiohttp.ClientSession at call sites
"""
from __future__ import annotations

import logging
import os

import aiohttp

log = logging.getLogger(__name__)

# Roles that may use bot.get_session() as a last resort (family-bot-5vw).
_BOT_SESSION_FALLBACK_ROLES = frozenset({"discord", "monolith"})

# Process-wide defaults so a hung peer cannot pin workers forever (family-bot-1bf.1).
# Per-request timeout= on get/post still overrides when callers need shorter budgets.
DEFAULT_CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=5, sock_read=20)
# Cross-container /internal/* should fail fast relative to the shared session default.
INTERNAL_POST_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=3, sock_read=8)


def make_shared_session() -> aiohttp.ClientSession:
    """Create the process-lifetime ClientSession with default timeouts."""
    return aiohttp.ClientSession(timeout=DEFAULT_CLIENT_TIMEOUT)


def _session_usable(session: aiohttp.ClientSession | None) -> bool:
    if session is None or session.closed:
        return False
    loop = getattr(session, "_loop", None)
    return loop is None or not loop.is_closed()


def _allow_bot_session_fallback() -> bool:
    if os.environ.get("BERNIE_TESTING") == "1":
        return True
    role = os.environ.get("ROLE", "monolith").strip().lower() or "monolith"
    return role in _BOT_SESSION_FALLBACK_ROLES


def get_http_session() -> aiohttp.ClientSession:
    """Return the process-wide ClientSession from ServiceContainer."""
    try:
        from llm.runtime import get_container

        container = get_container()
        if container is not None and _session_usable(container.session):
            return container.session
    except Exception as exc:
        log.warning("get_http_session: container session unavailable (%s)", exc)

    if not _allow_bot_session_fallback():
        role = os.environ.get("ROLE", "monolith")
        raise RuntimeError(
            f"get_http_session: no ServiceContainer.session for ROLE={role!r}; "
            "api/cognition must init llm.runtime with a live ClientSession"
        )

    import bot as bot_module

    return bot_module.get_session()
