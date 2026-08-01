"""Cross-container DB write client (discord/api → cognition).

When ROLE is cognition or monolith, writes execute locally (in-process). Split
compose sets ROLE=discord|api — those roles POST to bernie-cognition:9000.
Monolith short-circuit is for rollback/dev; RO ./data mounts (40A-5) are what
finally prevent discord/api from opening SQLite directly.

RPC retry (discord/api only): default up to 8 attempts on connection errors
(ClientConnectorError / OSError), backoff 2s × attempt. family-bot-29q:
UX-critical ops use a shorter budget (3 attempts, ~1s×attempt); best-effort
logs use 2 attempts so cognition blips cannot hang chat/presence ~72s.
HTTP 4xx/5xx and unknown ops fail immediately with RuntimeError — no local
fallback on split roles.

Uses one long-lived aiohttp.ClientSession per process (not per write).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Literal

import aiohttp

from cognition_write import execute_write_op

log = logging.getLogger(__name__)


def _json_default(obj: Any) -> Any:
    """Serialize datetimes in cognition RPC payloads (e.g. store_draft)."""
    from datetime import date, datetime

    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__!r} is not JSON serializable")


def _serialize_rpc_value(val: Any) -> Any:
    """Recursively coerce datetimes inside RPC kwargs (nested draft dicts, etc.)."""
    from datetime import date, datetime

    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, dict):
        return {k: _serialize_rpc_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_serialize_rpc_value(v) for v in val]
    return val

_rpc_session: aiohttp.ClientSession | None = None
_rpc_session_lock = asyncio.Lock()

_RPC_MAX_ATTEMPTS = 8
# family-bot-29q: fail-fast budgets (connection retries only)
_RPC_UX_MAX_ATTEMPTS = 3
_RPC_BEST_EFFORT_MAX_ATTEMPTS = 2

# Ops that must not hang the family shell / chat / presence path
_UX_CRITICAL_OPS = frozenset({
    "add_message",
    "add_chat_message",
    "create_chat_thread",
    "update_chat_thread_title",
    "delete_chat_thread",
    "update_presence",
    "apply_presence_tick",
    "set_last_home_signal",
    "save_rsvp",
    "mark_reminder_sent",
})

# Best-effort telemetry — never worth multi-minute RPC storms
_BEST_EFFORT_OPS = frozenset({
    "log_activity",
    "log_token_usage",
})

RpcClass = Literal["default", "ux", "batch"]


def _retry_budget(op: str, *, required: bool, rpc_class: RpcClass | None) -> tuple[int, float]:
    """Return (max_attempts, base_backoff_s) for connection retries.

    rpc_class is control-plane only — must NOT collide with write kwargs like
    create_task(priority=...) or create_cognitive_task(priority=10).
    """
    p = rpc_class
    if p is None:
        if not required or op in _BEST_EFFORT_OPS:
            p = "batch"
        elif op in _UX_CRITICAL_OPS:
            p = "ux"
        else:
            p = "default"
    if p == "ux":
        return _RPC_UX_MAX_ATTEMPTS, 1.0
    if p == "batch":
        return _RPC_BEST_EFFORT_MAX_ATTEMPTS, 0.5
    return _RPC_MAX_ATTEMPTS, 2.0


def _writer_role() -> str:
    return os.environ.get("ROLE", "monolith")


def writes_locally() -> bool:
    return _writer_role() in ("cognition", "monolith")


# Back-compat for tests/callers
_writes_locally = writes_locally


def _internal_cognition_base() -> str:
    env_url = os.environ.get("INTERNAL_COGNITION_URL")
    if env_url:
        return env_url.rstrip("/")
    try:
        from config import load_config

        cfg_url = load_config().get("internal_cognition_url")
        if cfg_url:
            return str(cfg_url).rstrip("/")
    except Exception:
        pass
    return "http://bernie-cognition:9000"


def internal_cognition_write_url() -> str:
    return f"{_internal_cognition_base()}/internal/db/write"


def internal_cognition_health_url() -> str:
    return f"{_internal_cognition_base()}/internal/db/health"


async def get_rpc_session() -> aiohttp.ClientSession:
    """Shared client session for cognition RPC (one pool per process)."""
    global _rpc_session
    async with _rpc_session_lock:
        if _rpc_session is not None and not _rpc_session.closed:
            loop = getattr(_rpc_session, "_loop", None)
            if loop is None or not loop.is_closed():
                return _rpc_session
            _rpc_session = None
        _rpc_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
    return _rpc_session


async def close_rpc_session() -> None:
    global _rpc_session
    if _rpc_session is not None and not _rpc_session.closed:
        await _rpc_session.close()
    _rpc_session = None


async def wait_for_cognition_writer(*, timeout_s: float = 90.0) -> bool:
    """Block until cognition /internal/db/health responds (discord/api startup)."""
    if writes_locally():
        return True
    url = internal_cognition_health_url()
    deadline = asyncio.get_running_loop().time() + timeout_s
    session = await get_rpc_session()
    while asyncio.get_running_loop().time() < deadline:
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return True
        except (aiohttp.ClientError, OSError):
            pass
        await asyncio.sleep(2)
    log.warning("cognition writer not ready after %.0fs (%s)", timeout_s, url)
    return False


async def cognition_db_write(
    op: str,
    *,
    required: bool = True,
    rpc_class: RpcClass | None = None,
    **kwargs: Any,
) -> Any:
    """Execute an allowlisted DB write on the cognition writer.

    rpc_class: "ux" (short retries), "batch" (telemetry), "default" (full budget).
    When None, inferred from op + required (family-bot-29q).
    Named rpc_class so domain kwargs like priority= still pass through.
    """
    kwargs = {k: _serialize_rpc_value(v) for k, v in kwargs.items()}
    if writes_locally():
        return await execute_write_op(op, kwargs)

    secret = os.environ.get("INTERNAL_POST_SECRET")
    headers: dict[str, str] = {}
    if secret:
        headers["X-Internal-Auth"] = secret

    payload = {"op": op, "kwargs": kwargs}
    url = internal_cognition_write_url()
    session = await get_rpc_session()
    last_err: Exception | None = None
    body = json.dumps(payload, default=_json_default)
    max_attempts, base_backoff = _retry_budget(op, required=required, rpc_class=rpc_class)

    for attempt in range(max_attempts):
        try:
            async with session.post(
                url, data=body, headers={**headers, "Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    err = RuntimeError(
                        f"cognition_db_write failed ({resp.status}) for op={op!r} at "
                        f"{url}: {text}. "
                        "Is bernie-cognition running and reachable on bernie-net?"
                    )
                    if not required:
                        log.warning("%s", err)
                        return None
                    raise err
                data = await resp.json()
                if not data.get("success"):
                    err = RuntimeError(f"cognition_db_write unexpected response: {data}")
                    if not required:
                        log.warning("%s", err)
                        return None
                    raise err
                return data.get("result")
        except (aiohttp.ClientConnectorError, aiohttp.ClientOSError, OSError) as e:
            last_err = e
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_backoff * (attempt + 1))
                continue
            err = RuntimeError(
                f"cognition_db_write could not reach cognition for op={op!r} at {url}: {e}"
            )
            if not required:
                log.warning("%s", err)
                return None
            raise err from e

    if not required:
        log.warning("cognition_db_write failed for op=%r", op)
        return None
    raise RuntimeError(f"cognition_db_write failed for op={op!r}") from last_err


async def cognition_db_write_best_effort(op: str, **kwargs: Any) -> Any:
    """Non-critical writes — log and return None on failure (short batch budget)."""
    return await cognition_db_write(op, required=False, rpc_class="batch", **kwargs)
