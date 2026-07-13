"""
Shadow evaluation firing (Phase 4.3 Session 1).

Carved from bot/eval_service.py. The original eval_service.py is retained as a
thin facade (imports + re-exports) so that all pre-existing call sites and
test patches of the form `from eval_service import fire_shadow_call` or
`patch("eval_service.fire_shadow_call")` continue to work with zero changes
to callers (including claude_service._maybe_fire_shadow).

Only shadow fire + supporting _call_*_shadow + _build + _log_to_langfuse moved.
Judges, HITL, nightly_eval_worker, ungrounded audit, weekly report etc. remain
in eval_service.py for Session 2+.

All tool invocations still go through ToolGateway per CLAUDE.md (the harness
path uses SmolExecutor which goes through the gateway with shadow=True).
"""
import asyncio
import hashlib
import os
from datetime import datetime, timezone

from contextlib import asynccontextmanager

import logging

import aiohttp

from http_session import get_http_session

from db_binding import get_database
import db_writes
from llm.hashing import hashable_system_prefix
from telemetry import fire_and_forget
from eval._http import (
    ANTHROPIC_KEY,
    _LF_HOST,
    _LF_PUBLIC,
    _LF_SECRET,
    _session_or_new,
    _ssl_for,
)

log = logging.getLogger(__name__)


def _shed_shadow_first(config: dict, shed_on_backpressure: bool | None) -> bool:
    if shed_on_backpressure is not None:
        return shed_on_backpressure
    from eval.policy import resolve_eval_policy
    return resolve_eval_policy(config).shed_on_backpressure


@asynccontextmanager
async def _shadow_queue_slot(
    config: dict,
    *,
    shadow: bool,
    shed_on_backpressure: bool | None = None,
):
    from llm.queue import get_default_queue

    q = get_default_queue()
    await q.configure(
        max_depth=int(config.get("executor", {}).get("llm_queue_max_depth", 4)),
        shed_shadow_first=_shed_shadow_first(config, shed_on_backpressure),
    )
    async with q.slot(shadow=shadow):
        yield


# ── Shadow Call Coroutine ─────────────────────────────────────────────────────

async def fire_shadow_call(
    *,
    user_message: str,
    system_prompt: str | list[dict],
    history: list[dict],
    primary_response: str,
    shadow_model: str,
    config: dict,
    channel_id: str = "",
    actor_id: str = "",
    db_module=None,
    session: aiohttp.ClientSession | None = None,
    shed_on_backpressure: bool | None = None,
) -> None:
    """Fire a text-only shadow model call and persist the result.

    Designed to be launched with `asyncio.create_task()` — failures are
    logged but never propagate to the caller.
    """
    db_module = db_module or get_database()

    try:
        from eval.policy import resolve_eval_policy
        policy = resolve_eval_policy(config)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cap = policy.shadow_daily_cap
        count = await db_module.get_shadow_call_count_today(today_str)
        if count >= cap:
            log.debug("shadow daily cap reached (%d/%d), skipping", count, cap)
            return

        prompt_hash = hashlib.sha256(
            (user_message + hashable_system_prefix(system_prompt)).encode()
        ).hexdigest()[:16]

        # Build minimal messages for Anthropic text-only call (no tools)
        messages = _build_shadow_messages(history, user_message)
        if not messages:
            return

        # Route: Ollama → local, LiteLLM → litellm.example.local proxy, else → Anthropic direct
        ollama_models = config.get("ollama_models", [])
        litellm_models = config.get("litellm_models", [])
        t_start = datetime.now(timezone.utc).timestamp()
        ollama_duration_ms = None
        if shadow_model in ollama_models:
            shadow_response, tok_in, tok_out, ollama_duration_ms = await _call_ollama_shadow(
                shadow_model, system_prompt, messages, config, session=session,
            )
        elif shadow_model in litellm_models:
            shadow_response, tok_in, tok_out = await _call_litellm_shadow(
                shadow_model, system_prompt, messages, config, session=session,
                shed_on_backpressure=shed_on_backpressure,
            )
        else:
            shadow_response, tok_in, tok_out = await _call_shadow_model(
                shadow_model, system_prompt, messages, config=config, session=session,
                shed_on_backpressure=shed_on_backpressure,
            )
        wall_ms = int((datetime.now(timezone.utc).timestamp() - t_start) * 1000)
        # Prefer Ollama's own total_duration (more precise); fall back to wall-clock
        duration_ms = ollama_duration_ms if ollama_duration_ms is not None else wall_ms
        if not shadow_response:
            return

        # Compute cost from token counts + pricing table
        try:
            cost_usd = get_database()._token_cost(tok_in or 0, tok_out or 0, shadow_model) if (tok_in or tok_out) else None
        except Exception:
            cost_usd = None

        await db_module.store_shadow_call(
            shadow_model=shadow_model,
            prompt_hash=prompt_hash,
            primary_response=primary_response,
            shadow_response=shadow_response,
            channel_id=str(channel_id),
            actor_id=str(actor_id),
            user_message=user_message,
            tokens_in=tok_in,
            tokens_out=tok_out,
            duration_ms=duration_ms,
            cost_usd=cost_usd,
        )
        log.info("shadow call recorded: model=%s hash=%s tok=%s/%s dur=%dms cost=$%.5f",
                 shadow_model, prompt_hash, tok_in, tok_out, duration_ms, cost_usd or 0)

        # Log to LangFuse — include tokens + cost for unified observability
        await _log_to_langfuse(
            shadow_model, user_message, shadow_response, actor_id, prompt_hash,
            tokens_in=tok_in, tokens_out=tok_out, cost_usd=cost_usd,
        )
    except Exception:
        log.exception("fire_shadow_call failed (non-fatal)")


async def fire_shadow_triplet(
    *,
    user_message: str,
    system_prompt: str | list[dict],
    history: list[dict],
    primary_response: str,
    primary_model: str,
    shadow_model: str,
    config: dict,
    channel_id: str = "",
    actor_id: str = "",
    db_module=None,
    smol_executor=None,
    smol_messages: list[dict] | None = None,
    smol_system: str | list[dict] | None = None,
    smol_tools: list[dict] | None = None,
    smol_exec_config=None,
    session: aiohttp.ClientSession | None = None,
    shed_on_backpressure: bool | None = None,
) -> None:
    """Fire model_shadow (text-only) and harness_shadow (SmolExecutor) in parallel.

    Both legs are non-blocking — failures are logged but never propagate.
    All three responses are stored as a triplet for overnight three-way scoring.
    """
    db_module = db_module or get_database()

    try:
        from eval.policy import resolve_eval_policy
        policy = resolve_eval_policy(config)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cap = policy.shadow_daily_cap
        count = await db_module.get_shadow_call_count_today(today_str)
        if count >= cap:
            log.debug("shadow daily cap reached (%d/%d), skipping triplet", count, cap)
            return

        prompt_hash = hashlib.sha256(
            (user_message + hashable_system_prefix(system_prompt)).encode()
        ).hexdigest()[:16]

        messages = _build_shadow_messages(history, user_message)
        if not messages:
            return

        model_shadow_task = asyncio.create_task(
            _call_model_shadow(
                shadow_model, system_prompt, messages, config, session=session,
                shed_on_backpressure=shed_on_backpressure,
            )
        )

        harness_shadow_task = asyncio.create_task(
            _call_harness_shadow(
                smol_executor,
                smol_messages or messages,
                smol_system or system_prompt,
                smol_tools or [],
                smol_exec_config,
                shadow_model=shadow_model,
            )
        )

        model_shadow_resp, ms_tok_in, ms_tok_out = await model_shadow_task
        harness_shadow_resp, hs_tok_in, hs_tok_out = await harness_shadow_task

        if not model_shadow_resp and not harness_shadow_resp:
            return

        # Log individual legs to token_usage for dashboards (in addition to store_shadow_triplet)
        try:
            if ms_tok_in or ms_tok_out:
                fire_and_forget(db_writes.routed("log_token_usage", 
                    input_tokens=ms_tok_in or 0,
                    output_tokens=ms_tok_out or 0,
                    model=shadow_model,
                    triggered_by="shadow:model",
                    conversation_id=None,
                    surface="shadow",
                ))

            # Harness token usage is logged by SmolExecutor._log_smol_generation
            # with surface="shadow_harness". Avoid double-counting here.
        except Exception:
            log.debug("shadow leg token_usage logging failed (non-fatal)", exc_info=True)

        # Total cost = model_shadow leg + harness_shadow leg. Both now share
        # the same shadow_model after harness was pinned, but the harness leg
        # uses many more tokens because it runs the full tool loop.
        try:
            _tc = get_database()._token_cost
            ms_cost = _tc(ms_tok_in or 0, ms_tok_out or 0, shadow_model) if (ms_tok_in or ms_tok_out) else 0.0
            hs_cost = _tc(hs_tok_in or 0, hs_tok_out or 0, shadow_model) if (hs_tok_in or hs_tok_out) else 0.0
            total_cost = (ms_cost or 0) + (hs_cost or 0)
            total_tok_in = (ms_tok_in or 0) + (hs_tok_in or 0)
            total_tok_out = (ms_tok_out or 0) + (hs_tok_out or 0)
        except Exception:
            total_cost = None
            total_tok_in = ms_tok_in
            total_tok_out = ms_tok_out

        await db_module.store_shadow_triplet(
            primary_response=primary_response,
            model_shadow_response=model_shadow_resp or "",
            harness_shadow_response=harness_shadow_resp or "",
            shadow_model=shadow_model,
            primary_model=primary_model,
            prompt_hash=prompt_hash,
            channel_id=str(channel_id),
            actor_id=str(actor_id),
            user_message=user_message,
            surface="chat",
            tokens_in=total_tok_in,
            tokens_out=total_tok_out,
            cost_usd=total_cost,
        )
        log.info("shadow triplet recorded: hash=%s", prompt_hash)

    except Exception:
        log.exception("fire_shadow_triplet failed (non-fatal)")


async def _call_model_shadow(
    model: str,
    system: str,
    messages: list[dict],
    config: dict,
    session: aiohttp.ClientSession | None = None,
    shed_on_backpressure: bool | None = None,
) -> tuple[str, int, int]:
    """Call the shadow model text-only (no tools). Returns (response, tok_in, tok_out)."""
    ollama_models = config.get("ollama_models", [])
    litellm_models = config.get("litellm_models", [])
    if model in ollama_models:
        resp, tok_in, tok_out, _ = await _call_ollama_shadow(model, system, messages, config, session=session)
    elif model in litellm_models:
        resp, tok_in, tok_out = await _call_litellm_shadow(
            model, system, messages, config, session=session,
            shed_on_backpressure=shed_on_backpressure,
        )
    else:
        resp, tok_in, tok_out = await _call_shadow_model(
            model, system, messages, config=config, session=session,
            shed_on_backpressure=shed_on_backpressure,
        )
    return resp or "", tok_in or 0, tok_out or 0


async def _call_harness_shadow(
    smol_executor,
    messages: list[dict],
    system: str,
    tools: list,
    exec_config,
    shadow_model: str | None = None,
) -> tuple[str, int, int]:
    """Run SmolExecutor as harness_shadow (shadow=True so writes are blocked by ToolGateway).

    The harness leg historically reused the primary model — which meant when the
    user was on Claude, every chat turn fired a SECOND Claude call invisibly.
    Pin to `shadow_model` instead so harness cost matches model_shadow cost.

    Returns (response, tok_in, tok_out). Tokens are read from the executor's
    accumulator (see `SmolExecutor.run`); 0 if smol didn't capture them.
    """
    try:
        if smol_executor is None:
            return "", 0, 0
        # Override exec_config.model to the shadow model so harness ≠ primary.
        if shadow_model and shadow_model != exec_config.model:
            from dataclasses import replace
            exec_config = replace(exec_config, model=shadow_model)
        result, tok_in, tok_out, _cc, _cr = await smol_executor._run_core(
            messages, system, tools, exec_config
        )
        return result or "", tok_in, tok_out
    except Exception:
        log.exception("_call_harness_shadow failed")
        return "", 0, 0


def _build_shadow_messages(history: list[dict], user_message: str) -> list[dict]:
    """Build a minimal message list for the shadow call, text-only."""
    msgs: list[dict] = []
    for m in history[-6:]:
        content = m.get("content", "")
        if isinstance(content, list):
            text_parts = [
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            content = " ".join(text_parts)
        if content and isinstance(content, str) and content.strip():
            msgs.append({"role": m["role"], "content": content.strip()})
    if user_message and user_message.strip():
        msgs.append({"role": "user", "content": user_message.strip()})

    # Ensure alternating roles starting with user
    final: list[dict] = []
    for m in msgs:
        if not final:
            if m["role"] == "user":
                final.append(m)
            continue
        if m["role"] == final[-1]["role"]:
            final[-1]["content"] += "\n" + m["content"]
        else:
            final.append(m)
    return final


async def _call_shadow_model(
    model: str, system: str, messages: list[dict],
    config: dict | None = None,
    session: aiohttp.ClientSession | None = None,
    shed_on_backpressure: bool | None = None,
) -> tuple[str | None, int | None, int | None]:
    """Direct Anthropic API call — text-only, no tools. Returns (text, tok_in, tok_out)."""
    if not ANTHROPIC_KEY:
        log.warning("shadow call skipped: ANTHROPIC_API_KEY not set")
        return None, None, None

    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 512,
        "system": system[:2000],
        "messages": messages,
    }
    async def _post() -> tuple[str | None, int | None, int | None]:
        async with _session_or_new(session) as sess:
            async with sess.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    log.warning("shadow call HTTP %d", resp.status)
                    return None, None, None
                body = await resp.json()
                content = body.get("content") or []
                text = content[0].get("text", "").strip() if content else None
                usage = body.get("usage", {})
                return text, usage.get("input_tokens"), usage.get("output_tokens")
    try:
        if config is not None:
            async with _shadow_queue_slot(
                config, shadow=True, shed_on_backpressure=shed_on_backpressure,
            ):
                return await _post()
        return await _post()
    except RuntimeError as exc:
        if str(exc) == "shed":
            log.debug("shadow model call shed by llm queue")
            return None, None, None
        raise
    except Exception:
        log.exception("shadow model API call failed")
        return None, None, None


async def _call_litellm_shadow(
    model: str, system: str, messages: list[dict], config: dict,
    session: aiohttp.ClientSession | None = None,
    shed_on_backpressure: bool | None = None,
) -> tuple[str | None, int | None, int | None]:
    """Proxy/OpenRouter shadow call for or-* aliases. Returns (text, tok_in, tok_out)."""
    from openrouter_models import (
        OPENROUTER_API_BASE,
        openrouter_api_key,
        openrouter_direct_enabled,
        resolve_openrouter_slug,
    )

    if openrouter_direct_enabled(config):
        base_url = OPENROUTER_API_BASE
        api_key = openrouter_api_key(config)
        model_id = resolve_openrouter_slug(model, config)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            "HTTP-Referer": "https://bernie.lan",
            "X-Title": "Bernie Family Bot",
        }
    else:
        base_url = config.get("litellm_base_url", "https://litellm.example.local")
        api_key = os.environ.get("LTE_LLM_MASTER_KEY", "")
        model_id = model
        headers = {
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
    oai_messages = [{"role": "system", "content": system[:2000]}] + messages
    payload = {
        "model": model_id,
        "max_tokens": 512,
        "messages": oai_messages,
    }
    async def _post() -> tuple[str | None, int | None, int | None]:
        async with _session_or_new(session) as sess:
            async with sess.post(
                f"{base_url}/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
                ssl=_ssl_for(base_url),
            ) as resp:
                if resp.status != 200:
                    log.warning("litellm shadow call HTTP %d for model=%s", resp.status, model)
                    return None, None, None
                body = await resp.json()
                choices = body.get("choices") or []
                text = (choices[0].get("message", {}).get("content") or "").strip() if choices else None
                usage = body.get("usage", {})
                return text, usage.get("prompt_tokens"), usage.get("completion_tokens")
    try:
        async with _shadow_queue_slot(
            config, shadow=True, shed_on_backpressure=shed_on_backpressure,
        ):
            return await _post()
    except RuntimeError as exc:
        if str(exc) == "shed":
            log.debug("litellm shadow call shed by llm queue")
            return None, None, None
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        # Free-tier models routinely time out — shadow is non-critical, downgrade to warning.
        log.warning("litellm shadow call timed out for model=%s: %s", model, exc)
        return None, None, None
    except Exception:
        log.exception("litellm shadow model call failed for model=%s", model)
        return None, None, None


async def _call_ollama_shadow(
    model: str, system: str, messages: list[dict], config: dict,
    session: aiohttp.ClientSession | None = None,
) -> tuple[str | None, int | None, int | None, int | None]:
    """Call local Ollama for shadow comparison — zero API cost. Returns (text, tok_in, tok_out, duration_ms)."""
    base_url = config.get("ollama_base_url", "http://192.168.1.X:11434")  # placeholder; set in config.json
    url = f"{base_url.rstrip('/')}/api/chat"

    ollama_messages = [{"role": "system", "content": system[:2000]}]
    for m in messages:
        ollama_messages.append({"role": m["role"], "content": m["content"]})

    payload = {
        "model": model,
        "messages": ollama_messages,
        "stream": False,
    }
    try:
        async with _session_or_new(session) as sess:
            async with sess.post(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
                ssl=_ssl_for(base_url),
            ) as resp:
                if resp.status != 200:
                    log.warning("ollama shadow call HTTP %d", resp.status)
                    return None, None, None, None
                data = await resp.json()
                text = (data.get("message", {}).get("content") or "").strip() or None
                # Ollama telemetry: prompt_eval_count=input tokens, eval_count=output tokens
                # total_duration is nanoseconds; convert to ms for duration_ms
                tok_in = data.get("prompt_eval_count") or None   # 0 means cached/unknown → None
                tok_out = data.get("eval_count") or None
                ollama_ms = data.get("total_duration")
                if ollama_ms:
                    ollama_ms = ollama_ms // 1_000_000  # ns → ms
                return text, tok_in, tok_out, ollama_ms
    except Exception:
        log.exception("ollama shadow call failed")
        return None, None, None, None


async def _log_to_langfuse(
    model: str, user_input: str, output: str,
    user_id: str, prompt_hash: str,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    session_id: str | None = None,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> None:
    """Log a shadow call to LangFuse as a trace+generation (non-fatal)."""
    if not _LF_PUBLIC or not _LF_SECRET or not _LF_HOST:
        return
    import base64 as _b64
    import uuid

    trace_id = uuid.uuid4().hex
    creds = _b64.b64encode(f"{_LF_PUBLIC}:{_LF_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
    }

    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "batch": [
            {
                "id": trace_id,
                "type": "trace-create",
                "timestamp": now_iso,
                "body": {
                    "id": trace_id,
                    "name": "shadow_eval",
                    "userId": user_id,
                    "sessionId": session_id,
                    "input": user_input[:500],
                    "output": output[:500],
                    "metadata": {
                        "source": "shadow_eval",
                        "model": model,
                        "prompt_hash": prompt_hash,
                    },
                    "tags": ["shadow_eval"],
                },
            },
            {
                "id": uuid.uuid4().hex,
                "type": "generation-create",
                "timestamp": now_iso,
                "body": {
                    "traceId": trace_id,
                    "name": f"shadow/{model}",
                    "model": model,
                    "input": user_input[:500],
                    "output": output[:500],
                    "metadata": {"source": "shadow_eval"},
                    **({
                        "usage": {
                            "input": tokens_in,
                            "output": tokens_out,
                            "unit": "TOKENS",
                        }
                    } if tokens_in is not None else {}),
                    "usageDetails": {
                        "input": int(tokens_in or 0),
                        "output": int(tokens_out or 0),
                        "cache_creation_input_tokens": int(cache_creation_tokens or 0),
                        "cache_read_input_tokens": int(cache_read_tokens or 0),
                    },
                    **({
                        "costDetails": {"total": cost_usd}
                    } if cost_usd is not None else {}),
                },
            },
        ],
    }
    try:
        session = get_http_session()
        async with session.post(
            f"{_LF_HOST}/api/public/ingestion",
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status >= 300:
                log.warning("langfuse shadow trace HTTP %d", resp.status)
    except Exception:
        log.debug("langfuse shadow trace failed (non-fatal)", exc_info=True)
