"""Shared Langfuse generation logger.

One async helper used by every LLM call site (Anthropic SDK, raw Anthropic
HTTP, Ollama, vision, eval judge, audit synth, nightly digest, etc.) so all
telemetry lands in Langfuse with a uniform shape.

Writes a `trace-create` + `generation-create` event pair to the self-hosted
Langfuse `/api/public/ingestion` endpoint. Non-fatal: any failure logs at
DEBUG and returns silently.

Required env: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST.
If any of the three is missing, calls are no-ops.
"""

import base64
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp

from http_session import get_http_session

log = logging.getLogger("langfuse_logger")


def new_trace_id() -> str:
    """Return a fresh Langfuse-compatible hex trace id."""
    return uuid.uuid4().hex


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"


def _creds_and_host() -> tuple[str, str] | None:
    lf_public = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    lf_secret = os.environ.get("LANGFUSE_SECRET_KEY", "")
    lf_host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    if not lf_public or not lf_secret or not lf_host:
        return None
    creds = base64.b64encode(f"{lf_public}:{lf_secret}".encode()).decode()
    return creds, lf_host


async def _post_ingestion(creds: str, lf_host: str, batch: list[dict]) -> None:
    try:
        sess = get_http_session()
        async with sess.post(
            f"{lf_host}/api/public/ingestion",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
            },
            json={"batch": batch},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status >= 300:
                log.warning("langfuse ingestion HTTP %d", resp.status)
    except Exception:
        log.debug("langfuse ingestion failed (non-fatal)", exc_info=True)


async def create_trace(
    *,
    name: str,
    actor_id: str = "",
    session_id: str | None = None,
    metadata: dict | None = None,
    tags: list[str] | None = None,
    user_input: str = "",
    output: str = "",
    trace_id: str | None = None,
) -> str | None:
    """Create a parent route trace. Returns trace_id or None when Langfuse disabled."""
    auth = _creds_and_host()
    if auth is None:
        return None
    creds, lf_host = auth
    tid = trace_id or new_trace_id()
    now_iso = _iso_z(datetime.now(timezone.utc))
    merged_meta = dict(metadata or {})
    merged_tags = list(tags or [])
    if name not in merged_tags:
        merged_tags.append(name)
    await _post_ingestion(creds, lf_host, [
        {
            "id": tid,
            "type": "trace-create",
            "timestamp": now_iso,
            "body": {
                "id": tid,
                "name": name,
                "userId": str(actor_id) if actor_id else None,
                "sessionId": session_id,
                "input": (user_input or "")[:1000],
                "output": (output or "")[:1000],
                "metadata": merged_meta,
                "tags": merged_tags,
            },
        },
    ])
    return tid


async def log_generation(
    *,
    model: str,
    user_input: str,
    output: str,
    input_tokens: int | None = 0,
    output_tokens: int | None = 0,
    name: str = "chat",
    actor_id: str = "",
    triggered_by: str = "system",
    session_id: str | None = None,
    metadata: dict | None = None,
    tags: list[str] | None = None,
    cost_usd: float | None = None,
    cache_creation_tokens: int | None = 0,
    cache_read_tokens: int | None = 0,
    latency_ms: int | None = None,
    trace_id: str | None = None,
    create_trace: bool = True,
    include_usage: bool | None = None,
) -> str | None:
    """Fire-and-forget Langfuse generation (optional parent trace).

    Parameters
    ----------
    input_tokens / output_tokens
        When ``None``, usage fields are **omitted** (unknown usage). When an
        int (including 0), they are sent. Pass ``include_usage=False`` to force
        omission even if ints are provided.
    trace_id
        Attach this generation to an existing parent route trace. When set with
        ``create_trace=False``, only a generation observation is emitted.
    create_trace
        If True (default) also emit a trace-create (legacy single-call shape).
        Set False when a parent trace was already created for multi-attempt
        subscription chains.
    """
    auth = _creds_and_host()
    if auth is None:
        return None
    creds, lf_host = auth

    tid = trace_id or new_trace_id()
    end_dt = datetime.now(timezone.utc)
    if latency_ms and latency_ms > 0:
        start_dt = end_dt - timedelta(milliseconds=int(latency_ms))
    else:
        start_dt = end_dt
    start_iso = _iso_z(start_dt)
    end_iso = _iso_z(end_dt)
    now_iso = end_iso

    merged_meta = {"source": name, "triggered_by": triggered_by}
    if metadata:
        merged_meta.update(metadata)
    merged_tags = list(tags or [])
    if triggered_by and triggered_by not in merged_tags:
        merged_tags.append(triggered_by)
    if name not in merged_tags:
        merged_tags.append(name)

    # Langfuse persists a generation-create event as an "observation" only when
    # startTime is present. Without it the trace lands but the generation is
    # silently dropped — caused our earlier traces to have 0 observations.
    gen_body: dict = {
        "id": uuid.uuid4().hex,
        "traceId": tid,
        "name": f"{name}/{model}",
        "model": model,
        "startTime": start_iso,
        "endTime": end_iso,
        "input": (user_input or "")[:1000],
        "output": (output or "")[:1000],
        "metadata": merged_meta,
    }

    # Omit usage when unknown — never invent zeros for subscription/CLI paths.
    send_usage = include_usage if include_usage is not None else (
        input_tokens is not None or output_tokens is not None
    )
    if send_usage and (input_tokens is not None or output_tokens is not None):
        usage: dict = {"unit": "TOKENS"}
        details: dict = {}
        if input_tokens is not None:
            usage["input"] = int(input_tokens)
            details["input"] = int(input_tokens)
        if output_tokens is not None:
            usage["output"] = int(output_tokens)
            details["output"] = int(output_tokens)
        if cache_creation_tokens is not None:
            details["cache_creation_input_tokens"] = int(cache_creation_tokens)
        if cache_read_tokens is not None:
            details["cache_read_input_tokens"] = int(cache_read_tokens)
        gen_body["usage"] = usage
        if details:
            gen_body["usageDetails"] = details
    if cost_usd is not None:
        gen_body["costDetails"] = {"total": float(cost_usd)}

    batch: list[dict] = []
    if create_trace:
        batch.append({
            "id": tid,
            "type": "trace-create",
            "timestamp": now_iso,
            "body": {
                "id": tid,
                "name": name,
                "userId": str(actor_id) if actor_id else None,
                "sessionId": session_id,
                "input": (user_input or "")[:1000],
                "output": (output or "")[:1000],
                "metadata": merged_meta,
                "tags": merged_tags,
            },
        })
    batch.append({
        "id": uuid.uuid4().hex,
        "type": "generation-create",
        "timestamp": now_iso,
        "body": gen_body,
    })
    await _post_ingestion(creds, lf_host, batch)
    return tid
