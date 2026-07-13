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


async def log_generation(
    *,
    model: str,
    user_input: str,
    output: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    name: str = "chat",
    actor_id: str = "",
    triggered_by: str = "system",
    session_id: str | None = None,
    metadata: dict | None = None,
    tags: list[str] | None = None,
    cost_usd: float | None = None,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    latency_ms: int | None = None,
) -> None:
    """Fire-and-forget Langfuse trace+generation log.

    `name` becomes the trace name (e.g. "chat", "audit", "judge_pair",
    "vision", "worker_topic", "ollama_fallback").
    """
    lf_public = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    lf_secret = os.environ.get("LANGFUSE_SECRET_KEY", "")
    lf_host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    if not lf_public or not lf_secret or not lf_host:
        return

    trace_id = uuid.uuid4().hex
    creds = base64.b64encode(f"{lf_public}:{lf_secret}".encode()).decode()
    # Project standard: UTC ISO-8601 with `Z` suffix (see CLAUDE.md Date/Time Standards).
    # Langfuse accepts both `+00:00` and `Z`, so this is the house-style choice.
    def _iso_z(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"

    end_dt = datetime.now(timezone.utc)
    if latency_ms and latency_ms > 0:
        start_dt = end_dt - timedelta(milliseconds=int(latency_ms))
    else:
        start_dt = end_dt
    start_iso = _iso_z(start_dt)
    end_iso = _iso_z(end_dt)
    now_iso = end_iso  # legacy callers

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
    gen_body = {
        "id": uuid.uuid4().hex,
        "traceId": trace_id,
        "name": f"{name}/{model}",
        "model": model,
        "startTime": start_iso,
        "endTime": end_iso,
        "input": (user_input or "")[:1000],
        "output": (output or "")[:1000],
        "usage": {
            "input": int(input_tokens or 0),
            "output": int(output_tokens or 0),
            "unit": "TOKENS",
        },
        "metadata": {"source": name},
    }
    # Cache token detail (Langfuse v2 ingestion: usageDetails). Always emit
    # so Langfuse can aggregate accurately; 0 is fine for non-cache calls.
    gen_body["usageDetails"] = {
        "input": int(input_tokens or 0),
        "output": int(output_tokens or 0),
        "cache_creation_input_tokens": int(cache_creation_tokens or 0),
        "cache_read_input_tokens": int(cache_read_tokens or 0),
    }
    if cost_usd is not None:
        gen_body["costDetails"] = {"total": float(cost_usd)}

    payload = {
        "batch": [
            {
                "id": trace_id,
                "type": "trace-create",
                "timestamp": now_iso,
                "body": {
                    "id": trace_id,
                    "name": name,
                    "userId": str(actor_id) if actor_id else None,
                    "sessionId": session_id,
                    "input": (user_input or "")[:1000],
                    "output": (output or "")[:1000],
                    "metadata": merged_meta,
                    "tags": merged_tags,
                },
            },
            {
                "id": uuid.uuid4().hex,
                "type": "generation-create",
                "timestamp": now_iso,
                "body": gen_body,
            },
        ],
    }
    try:
        sess = get_http_session()
        async with sess.post(
                f"{lf_host}/api/public/ingestion",
                headers={
                    "Authorization": f"Basic {creds}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    log.warning("langfuse %s trace HTTP %d", name, resp.status)
    except Exception:
        log.debug("langfuse %s trace failed (non-fatal)", name, exc_info=True)
