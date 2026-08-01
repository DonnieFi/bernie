"""Execute subscription model completions with ordered fallbacks.

Chain (Grok / Codex primaries): subscription CLI runner → direct OpenRouter
twin → selected Ollama (surface-specific else global; hosts via ollama_resolver).

Text path for Grok (family-bot-tho.3). Tool envelopes are family-bot-tho.4.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Mapping

import aiohttp

from completion_router import (
    CompletionError,
    CompletionErrorCode,
    CompletionRequest,
    CompletionResult,
    TokenUsage,
    ToolCall,
    resolve_fallback_chain,
    subscription_model,
)

log = logging.getLogger(__name__)

_DEFAULT_RUNNER_TIMEOUT_S = 180
_DEFAULT_TOOL_TIMEOUT_S = 30


def _hop_timeout_s(budget: int | None, *, default: int) -> int:
    """Per-hop HTTP timeout from remaining budget (or default when unset)."""
    if budget is None:
        return default
    if budget <= 0:
        return 1
    return min(budget, default)


def _remaining_budget(budget: int | None, latency_ms: float | None) -> int | None:
    """Subtract hop latency from chain budget (minimum 1s charge per hop)."""
    if budget is None:
        return None
    spent = max(1, int((latency_ms or 0) / 1000))
    return max(0, budget - spent)

_RETRYABLE = frozenset({
    CompletionErrorCode.TIMEOUT,
    CompletionErrorCode.AUTH,
    CompletionErrorCode.QUOTA,
    CompletionErrorCode.UNAVAILABLE,
    CompletionErrorCode.BUSY,
    CompletionErrorCode.UPSTREAM,
})


def runner_base_url(cfg: Mapping[str, Any] | None = None) -> str:
    cfg = cfg or {}
    return (
        (cfg.get("subscription_runner_url") if isinstance(cfg, Mapping) else None)
        or os.environ.get("SUBSCRIPTION_RUNNER_URL")
        or "http://bernie-subscription-runner:8080"
    ).rstrip("/")


def _runner_auth_headers() -> dict[str, str]:
    secret = (os.environ.get("SUBSCRIPTION_RUNNER_SECRET") or "").strip()
    if secret:
        return {"X-Subscription-Runner-Auth": secret}
    return {}


def format_model_auth_message(provider: str, state: str) -> str:
    """Secret-safe Discord/tool text for a single subscription provider.

    Never includes device codes, tokens, auth-file paths, or account identity.
    """
    provider = (provider or "").strip().lower()
    state = (state or "unavailable").strip().lower()
    if state == "ready":
        return f"**{provider}:** ready (runner volume authenticated)."
    if state == "reauth-required":
        login_hint = (
            "./scripts/subscription_grok_login.sh"
            if provider == "grok"
            else "codex login --device-auth (subscription-runner volume)"
        )
        return (
            f"**{provider}:** reauth-required.\n"
            f"On BernieHost (SSH): `{login_hint}`.\n"
            "Do not paste tokens into Discord."
        )
    return f"**{provider}:** {state}. Ensure the subscription runner is up."


async def subscription_usage_snapshot(
    cfg: Mapping[str, Any] | None = None,
    *,
    provider: str | None = None,
) -> str:
    """Normalized usage text. Missing fields are explicitly unavailable (never fabricated)."""
    health = await fetch_runner_health(cfg)
    providers = (health or {}).get("providers") or {}
    lines: list[str] = []
    for key in ("codex", "grok"):
        if provider and key != provider:
            continue
        state = providers.get(key, "unavailable")
        # Official CLIs do not expose a stable quota API through the runner yet.
        # Never invent zeros — explicit unavailable markers only.
        lines.append(
            f"• {key}: readiness={state}; "
            f"usage=unavailable; quota=unavailable; reset=unavailable"
        )
    if not lines:
        return "No subscription providers configured."
    return "\n".join(lines)


async def fetch_runner_health(cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Secret-safe runner /health (readiness only). Never returns auth content."""
    base = runner_base_url(cfg)
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{base}/health", headers=_runner_auth_headers()) as resp:
                if resp.status != 200:
                    return {"status": "error", "providers": {"codex": "unavailable", "grok": "unavailable"}}
                data = await resp.json(content_type=None)
                if not isinstance(data, dict):
                    return {"status": "error", "providers": {}}
                # Strip anything unexpected — only status/providers/capacity.
                providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
                # Drop email/token/account/path fields if a misbehaving runner includes them.
                safe_providers = {
                    k: v for k, v in providers.items()
                    if k in ("codex", "grok") and v in ("ready", "unavailable", "reauth-required")
                }
                return {
                    "status": data.get("status", "ok"),
                    "providers": safe_providers,
                    "capacity": data.get("capacity") if isinstance(data.get("capacity"), dict) else {},
                }
    except Exception:
        return {"status": "error", "providers": {"codex": "unavailable", "grok": "unavailable"}}

def _error_from_body(body: Mapping[str, Any]) -> CompletionError | None:
    err = body.get("error")
    if not isinstance(err, Mapping):
        return None
    code_raw = str(err.get("code") or "upstream")
    try:
        code = CompletionErrorCode(code_raw)
    except ValueError:
        code = CompletionErrorCode.UPSTREAM
    return CompletionError(
        code=code,
        message=str(err.get("message") or "")[:500],
        retryable=bool(err.get("retryable", code in _RETRYABLE)),
    )


def _usage_from_body(body: Mapping[str, Any]) -> TokenUsage:
    usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else {}
    def _n(key: str) -> int | None:
        val = usage.get(key)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return None
        return int(val)
    return TokenUsage(input_tokens=_n("input_tokens"), output_tokens=_n("output_tokens"))


async def _post_runner(
    session: aiohttp.ClientSession,
    base: str,
    request: CompletionRequest,
) -> CompletionResult:
    started = time.monotonic()
    hop_timeout = _hop_timeout_s(request.timeout_s, default=_DEFAULT_RUNNER_TIMEOUT_S)
    schema = request.output_schema
    if schema:
        from json_schema_compat import sanitize_json_schema_for_structured_output

        schema = sanitize_json_schema_for_structured_output(schema)
    payload = {
        "provider": request.provider,
        "model": request.model,
        "surface": request.surface,
        "system": request.system,
        "prompt": request.prompt,
        "messages": list(request.messages),
        "tools": list(request.tools) if request.tools else [],
        "output_schema": schema,
        "timeout_s": hop_timeout,
        "continuation_id": request.continuation_id,
        "tool_results": list(request.tool_results),
    }
    url = f"{base}/v1/completions"
    try:
        async with session.post(
            url,
            json=payload,
            headers=_runner_auth_headers(),
            timeout=aiohttp.ClientTimeout(total=hop_timeout + 5),
        ) as resp:
            body = await resp.json(content_type=None)
            latency_ms = (time.monotonic() - started) * 1000.0
            if not isinstance(body, dict):
                return CompletionResult(
                    provider=request.provider,
                    model=request.model,
                    latency_ms=latency_ms,
                    error=CompletionError(
                        CompletionErrorCode.UPSTREAM,
                        "runner returned non-object",
                        retryable=True,
                    ),
                )
            if resp.status == 503:
                err = _error_from_body(body) or CompletionError(
                    CompletionErrorCode.BUSY, "runner busy", retryable=True
                )
                return CompletionResult(
                    provider=request.provider,
                    model=request.model,
                    latency_ms=latency_ms,
                    error=err,
                )
            if resp.status >= 400:
                err = _error_from_body(body) or CompletionError(
                    CompletionErrorCode.UPSTREAM,
                    f"runner HTTP {resp.status}",
                    retryable=True,
                )
                return CompletionResult(
                    provider=request.provider,
                    model=request.model,
                    latency_ms=latency_ms,
                    error=err,
                )
            embedded = _error_from_body(body)
            if embedded is not None:
                return CompletionResult(
                    provider=request.provider,
                    model=request.model,
                    latency_ms=float(body.get("latency_ms") or latency_ms),
                    error=embedded,
                    usage=_usage_from_body(body),
                )
            text = body.get("text")
            tool_calls: list[ToolCall] = []
            raw_calls = body.get("tool_calls") or []
            if isinstance(raw_calls, list):
                for call in raw_calls:
                    if not isinstance(call, Mapping):
                        continue
                    name = call.get("name")
                    args = call.get("arguments")
                    if isinstance(name, str) and isinstance(args, Mapping):
                        tool_calls.append(ToolCall(
                            id=str(call.get("id") or name),
                            name=name,
                            arguments=dict(args),
                        ))
            return CompletionResult(
                provider=request.provider,  # type: ignore[arg-type]
                model=str(body.get("model") or request.model),
                text=None if text is None else str(text),
                tool_calls=tuple(tool_calls),
                usage=_usage_from_body(body),
                latency_ms=float(body.get("latency_ms") or latency_ms),
                continuation_id=(
                    str(body["continuation_id"])
                    if body.get("continuation_id") else None
                ),
            )
    except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
        return CompletionResult(
            provider=request.provider,
            model=request.model,
            latency_ms=(time.monotonic() - started) * 1000.0,
            error=CompletionError(
                CompletionErrorCode.TIMEOUT if "timeout" in type(exc).__name__.lower()
                else CompletionErrorCode.UNAVAILABLE,
                f"runner unreachable: {type(exc).__name__}",
                retryable=True,
            ),
        )


async def _openrouter_text(
    session: aiohttp.ClientSession,
    model: str,
    request: CompletionRequest,
    cfg: Mapping[str, Any],
) -> CompletionResult:
    """Direct OpenRouter chat/completions (bypasses LiteLLM)."""
    started = time.monotonic()
    api_key = (
        os.environ.get("OPENROUTER_API_KEY", "").strip()
        or os.environ.get("OPENROUTER_API_KEY_LITE", "").strip()
    )
    if not api_key:
        return CompletionResult(
            provider="openrouter",
            model=model,
            error=CompletionError(
                CompletionErrorCode.UNAVAILABLE,
                "OPENROUTER_API_KEY not set",
                retryable=True,
            ),
        )

    messages: list[dict[str, str]] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    for msg in request.messages:
        if not isinstance(msg, Mapping):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        if content is None:
            continue
        messages.append({"role": role, "content": str(content)})
    if request.prompt and not any(m.get("role") == "user" for m in messages):
        messages.append({"role": "user", "content": request.prompt})
    if not messages:
        return CompletionResult(
            provider="openrouter",
            model=model,
            error=CompletionError(
                CompletionErrorCode.INVALID_REQUEST,
                "no messages for openrouter",
                retryable=False,
            ),
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://bernie.lan",
        "X-Title": "Bernie Family Bot",
    }
    # OpenRouter accepts either alias or full slug; prefer configured slug.
    from openrouter_models import resolve_openrouter_slug

    or_model = resolve_openrouter_slug(model, dict(cfg))
    body: dict[str, Any] = {"model": or_model, "messages": messages, "max_tokens": 2048}
    if request.output_schema:
        from json_schema_compat import sanitize_json_schema_for_structured_output

        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "bernie_output",
                "strict": True,
                "schema": sanitize_json_schema_for_structured_output(request.output_schema),
            },
        }
    try:
        async with session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=_hop_timeout_s(request.timeout_s, default=120)),
        ) as resp:
            data = await resp.json(content_type=None)
            latency_ms = (time.monotonic() - started) * 1000.0
            if resp.status == 429:
                return CompletionResult(
                    provider="openrouter",
                    model=model,
                    latency_ms=latency_ms,
                    error=CompletionError(CompletionErrorCode.QUOTA, "openrouter 429", retryable=True),
                )
            if resp.status in (401, 403):
                return CompletionResult(
                    provider="openrouter",
                    model=model,
                    latency_ms=latency_ms,
                    error=CompletionError(CompletionErrorCode.AUTH, f"openrouter {resp.status}", retryable=True),
                )
            if resp.status >= 400 or not isinstance(data, dict):
                return CompletionResult(
                    provider="openrouter",
                    model=model,
                    latency_ms=latency_ms,
                    error=CompletionError(
                        CompletionErrorCode.UPSTREAM,
                        f"openrouter HTTP {resp.status}",
                        retryable=True,
                    ),
                )
            choices = data.get("choices") or []
            text = None
            if choices and isinstance(choices[0], Mapping):
                message = choices[0].get("message") or {}
                if isinstance(message, Mapping):
                    text = message.get("content")
            usage_raw = data.get("usage") if isinstance(data.get("usage"), Mapping) else {}
            def _n(key: str) -> int | None:
                val = usage_raw.get(key)
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    return None
                return int(val)
            return CompletionResult(
                provider="openrouter",
                model=model,
                text=None if text is None else str(text),
                usage=TokenUsage(
                    input_tokens=_n("prompt_tokens"),
                    output_tokens=_n("completion_tokens"),
                ),
                latency_ms=latency_ms,
            )
    except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
        return CompletionResult(
            provider="openrouter",
            model=model,
            latency_ms=(time.monotonic() - started) * 1000.0,
            error=CompletionError(
                CompletionErrorCode.TIMEOUT if "timeout" in type(exc).__name__.lower()
                else CompletionErrorCode.UNAVAILABLE,
                f"openrouter unreachable: {type(exc).__name__}",
                retryable=True,
            ),
        )


async def _ollama_text(
    request: CompletionRequest,
    cfg: Mapping[str, Any],
    model: str,
    session: aiohttp.ClientSession | None,
) -> CompletionResult:
    """Ollama hop without nested log_llm_turn — chain telemetry owns observations."""
    started = time.monotonic()
    try:
        from llm.ollama import call_ollama
        from http_session import get_http_session
        sess = session or get_http_session()
        out = await call_ollama(
            system=request.system or "",
            messages=[dict(m) for m in request.messages],
            config=dict(cfg),
            session=sess,
            model_override=model,
            user_message=request.prompt or "",
            skip_telemetry=True,
            timeout_s=request.timeout_s,
        )
        if isinstance(out, tuple):
            text, usage = out
        else:
            text, usage = out, {}
        offline_markers = (
            "trouble reaching both my primary",
            "Everything is offline",
            "busy right now",
        )
        if not text or any(m in (text or "") for m in offline_markers):
            return CompletionResult(
                provider="ollama",
                model=model,
                text=None,
                latency_ms=(time.monotonic() - started) * 1000.0,
                error=CompletionError(
                    CompletionErrorCode.UPSTREAM,
                    "ollama returned no usable text",
                    retryable=True,
                ),
            )
        return CompletionResult(
            provider="ollama",
            model=model,
            text=text,
            usage=TokenUsage(
                input_tokens=usage.get("input_tokens") if isinstance(usage, Mapping) else None,
                output_tokens=usage.get("output_tokens") if isinstance(usage, Mapping) else None,
            ),
            latency_ms=(time.monotonic() - started) * 1000.0,
        )
    except Exception as exc:
        log.exception("ollama fallback failed")
        return CompletionResult(
            provider="ollama",
            model=model,
            latency_ms=(time.monotonic() - started) * 1000.0,
            error=CompletionError(
                CompletionErrorCode.UPSTREAM,
                f"ollama failed: {type(exc).__name__}",
                retryable=True,
            ),
        )


async def complete_subscription_chain(
    request: CompletionRequest,
    cfg: Mapping[str, Any],
    *,
    surface_ollama_model: str | None = None,
    session: aiohttp.ClientSession | None = None,
    skip_runner_hops: bool = False,
) -> tuple[CompletionResult, list[dict[str, Any]]]:
    """Run Grok/Codex primary with OpenRouter then Ollama fallbacks.

    Returns ``(final_result, attempts)`` where attempts is metadata for each hop
    (provider, model, status, error code, latency_ms) without secrets.
    """
    entry = subscription_model(request.model, cfg)
    if entry is None:
        raise ValueError(f"not a subscription model: {request.model}")

    try:
        chain = resolve_fallback_chain(
            entry.model, cfg, surface_ollama_model=surface_ollama_model
        )
    except ValueError as exc:
        err = CompletionResult(
            provider=entry.provider,
            model=entry.model,
            error=CompletionError(
                CompletionErrorCode.UNAVAILABLE,
                str(exc),
                retryable=False,
            ),
        )
        return err, []

    owns_session = False
    if session is None:
        try:
            from http_session import get_http_session
            session = get_http_session()
        except Exception:
            session = aiohttp.ClientSession()
            owns_session = True
    sess = session
    attempts: list[dict[str, Any]] = []
    try:
        return await _run_chain(
            request, entry, chain, sess, attempts, cfg,
            skip_runner_hops=skip_runner_hops,
        )
    finally:
        if owns_session and sess is not None:
            await sess.close()


async def _run_chain(
    request, entry, chain, sess, attempts, cfg, *, skip_runner_hops: bool = False,
):
    last: CompletionResult | None = None
    budget: int | None = request.timeout_s
    for attempt_idx, target in enumerate(chain):
        if skip_runner_hops and target.provider in ("grok", "codex"):
            attempts.append({
                "attempt": attempt_idx,
                "provider": target.provider,
                "model": target.model,
                "ok": False,
                "skipped": True,
                "error_code": "text_degrade_skip_runner",
                "selected_primary": entry.model,
            })
            continue
        # Tool loops must stay on subscription runner hops — never strip tools on OR/Ollama.
        if (
            request.tools or request.continuation_id
        ) and target.provider not in ("grok", "codex"):
            attempts.append({
                "attempt": attempt_idx,
                "provider": target.provider,
                "model": target.model,
                "ok": False,
                "skipped": True,
                "error_code": "tool_loop_no_fallback",
                "selected_primary": entry.model,
            })
            log.info(
                "subscription_chain skip hop=%s (tool loop cannot fall back to %s)",
                attempt_idx,
                target.provider,
            )
            continue
        hop_default = (
            _DEFAULT_RUNNER_TIMEOUT_S
            if target.provider in ("grok", "codex")
            else 120
        )
        hop_timeout = _hop_timeout_s(budget, default=hop_default)
        hop_req = CompletionRequest(
            surface=request.surface,
            provider=target.provider,  # type: ignore[arg-type]
            model=target.model,
            messages=request.messages,
            system=request.system,
            prompt=request.prompt,
            tools=request.tools if target.provider in ("grok", "codex") else (),
            output_schema=request.output_schema if target.provider in ("grok", "codex") else None,
            timeout_s=hop_timeout if budget is not None else request.timeout_s,
            continuation_id=request.continuation_id,
            tool_results=request.tool_results,
        )
        if target.provider in ("grok", "codex"):
            result = await _post_runner(sess, runner_base_url(cfg), hop_req)
        elif target.provider == "openrouter":
            result = await _openrouter_text(sess, target.model, hop_req, cfg)
        elif target.provider == "ollama":
            result = await _ollama_text(hop_req, cfg, target.model, sess)
        else:
            result = CompletionResult(
                provider=target.provider,  # type: ignore[arg-type]
                model=target.model,
                error=CompletionError(
                    CompletionErrorCode.INVALID_REQUEST,
                    f"unsupported chain provider {target.provider}",
                    retryable=False,
                ),
            )

        last = result
        attempts.append({
            "attempt": attempt_idx,
            "provider": target.provider,
            "model": target.model,
            "ok": result.error is None and (bool(result.text) or bool(result.tool_calls)),
            "error_code": result.error.code.value if result.error else None,
            "retryable": result.error.retryable if result.error else None,
            "latency_ms": result.latency_ms,
            "selected_primary": entry.model,
            "tool_calls": len(result.tool_calls),
            # Provider-reported only — leave None when unknown (never fabricate 0).
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        })
        log.info(
            "subscription_chain attempt=%s provider=%s model=%s ok=%s error=%s tools=%s",
            attempt_idx,
            target.provider,
            target.model,
            result.error is None and (bool(result.text) or bool(result.tool_calls)),
            result.error.code.value if result.error else None,
            len(result.tool_calls),
        )
        if result.error:
            log.warning(
                "subscription_chain hop failed provider=%s model=%s code=%s msg=%s",
                target.provider,
                target.model,
                result.error.code.value,
                (result.error.message or "")[:200],
            )

        if result.error is None and (result.text is not None or result.tool_calls):
            return result, attempts

        budget = _remaining_budget(budget, result.latency_ms)

        # Non-retryable → stop without silent provider switch for validation errors.
        if result.error and not result.error.retryable:
            return result, attempts
        # else fall through to next hop

    assert last is not None
    return last, attempts


def is_grok_subscription_model(model: str | None, cfg: Mapping[str, Any]) -> bool:
    entry = subscription_model(model, cfg)
    return entry is not None and entry.provider == "grok" and entry.enabled


def is_subscription_model(model: str | None, cfg: Mapping[str, Any]) -> bool:
    entry = subscription_model(model, cfg)
    return entry is not None and entry.enabled


async def log_subscription_attempts(
    attempts: list[dict[str, Any]],
    *,
    user_input: str = "",
    final_text: str = "",
    session_id: str | None = None,
    conversation_id: str | None = None,
    actor_id: str = "",
    triggered_by: str = "discord",
    surface: str = "chat",
) -> dict[str, Any]:
    """Persist telemetry for a subscription chain.

    Contract (family-bot-3an.4):
      * One parent Langfuse route trace for the whole chain.
      * Exactly one Langfuse generation/observation per attempt
        (never log_generation + log_llm_turn for the same hop — that doubles).
      * Provider-reported usage only: omit Langfuse usage fields when unknown;
        skip DB token_usage when both input/output are absent.
      * Metadata is secret-safe: no prompts, device codes, auth files, or
        account identity. Markers only for input/output bodies.

    Returns a small summary dict for tests (trace_id, observation count, db rows).
    """
    import db_writes
    from langfuse_logger import create_trace, log_generation, new_trace_id

    # Redact: never pass real prompts into Langfuse for subscription hops.
    _ = user_input  # intentionally unused (secret/prompt safety)
    summary: dict[str, Any] = {
        "trace_id": None,
        "observations": 0,
        "db_rows": 0,
        "providers": [],
    }
    if not attempts:
        return summary

    parent_meta = {
        "route": "subscription_chain",
        "surface": surface,
        "triggered_by": triggered_by,
        "attempt_count": str(len(attempts)),
        "selected_primary": str(attempts[0].get("selected_primary") or ""),
    }
    # Parent markers only — never prompts or credentials.
    parent_trace = await create_trace(
        name="subscription_route",
        actor_id=actor_id,
        session_id=session_id,
        metadata=parent_meta,
        tags=["subscription_chain", f"surface:{surface}"],
        user_input="[subscription-route]",
        output="[ok]" if final_text else "[empty]",
        trace_id=new_trace_id(),
    )
    # Even when Langfuse is disabled, keep a stable id for the summary.
    parent_trace = parent_trace or new_trace_id()
    summary["trace_id"] = parent_trace

    for att in attempts:
        provider = str(att.get("provider") or "unknown")
        model = str(att.get("model") or "unknown")
        ok = bool(att.get("ok"))
        err = att.get("error_code")
        latency = att.get("latency_ms")
        attempt_n = att.get("attempt")
        in_tok = att.get("input_tokens")
        out_tok = att.get("output_tokens")
        has_usage = isinstance(in_tok, int) and isinstance(out_tok, int)

        meta = {
            "route": "subscription_chain",
            "provider": provider,
            "actual_provider": provider,
            "actual_model": model,
            "attempt": str(attempt_n),
            "status": "ok" if ok else "error",
            "fallback_reason": str(err or ""),
            "selected_primary": str(att.get("selected_primary") or ""),
            "retryable": str(att.get("retryable")),
            "surface": surface,
        }
        # Guard: never leak secrets if a caller stuffed them into att.
        for banned in (
            "token", "secret", "password", "device_code", "auth_file",
            "email", "account", "authorization", "api_key", "prompt",
        ):
            meta.pop(banned, None)

        tags = [
            "subscription_chain",
            f"provider:{provider}",
            f"attempt:{attempt_n}",
            "ok" if ok else f"error:{err or 'unknown'}",
        ]
        # One generation under the parent route trace. Usage omitted when unknown.
        await log_generation(
            model=f"{provider}/{model}",
            user_input="[subscription-attempt]",
            output="[ok]" if ok else f"[error:{err}]",
            input_tokens=int(in_tok) if isinstance(in_tok, int) else None,
            output_tokens=int(out_tok) if isinstance(out_tok, int) else None,
            name="subscription_attempt",
            actor_id=actor_id,
            triggered_by=triggered_by,
            session_id=session_id,
            latency_ms=int(latency) if isinstance(latency, (int, float)) else None,
            metadata=meta,
            tags=tags,
            cost_usd=None,
            cache_creation_tokens=None,
            cache_read_tokens=None,
            trace_id=parent_trace,
            create_trace=False,
        )
        summary["observations"] += 1
        summary["providers"].append(provider)

        # DB only — never call log_llm_turn here (that would also hit Langfuse).
        if has_usage:
            try:
                await db_writes.routed(
                    "log_token_usage",
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    model=f"{provider}/{model}",
                    conversation_id=conversation_id,
                    triggered_by=triggered_by,
                    session_id=session_id,
                    surface=surface,
                )
                summary["db_rows"] += 1
            except Exception:
                log.exception("subscription attempt db log failed")

    return summary


async def complete_subscription_with_tools(
    request: CompletionRequest,
    cfg: Mapping[str, Any],
    *,
    execute_tool,
    max_steps: int = 5,
    session: aiohttp.ClientSession | None = None,
) -> tuple[CompletionResult, list[dict[str, Any]]]:
    """Multi-step subscription completion; tool calls go only through ``execute_tool``.

    ``execute_tool(name, args) -> str`` must enforce ToolGateway / permissions.
    At most ``max_steps`` model rounds (tool steps + final).
    """
    if max_steps < 1:
        max_steps = 1
    messages = list(request.messages)
    all_attempts: list[dict[str, Any]] = []
    last: CompletionResult | None = None
    budget = request.timeout_s
    nudged_step0 = False
    nudged_synthesis = False
    continuation_id: str | None = None
    pending_tool_results: tuple[Mapping[str, Any], ...] = ()

    for step in range(max_steps):
        if budget is not None and budget <= 0:
            return CompletionResult(
                provider=request.provider,
                model=request.model,
                error=CompletionError(
                    CompletionErrorCode.TIMEOUT,
                    "tool loop budget exhausted",
                    retryable=True,
                ),
            ), all_attempts
        step_req = CompletionRequest(
            surface=request.surface,
            provider=request.provider,
            model=request.model,
            messages=tuple(messages),
            system=request.system,
            prompt=request.prompt if step == 0 else None,
            # After tool results are in context, synthesis hop — no tool catalog (Grok
            # otherwise re-invokes tools or returns empty envelopes).
            tools=request.tools if step == 0 else (),
            output_schema=request.output_schema if step == 0 else None,
            timeout_s=budget,
            continuation_id=continuation_id,
            tool_results=pending_tool_results,
        )
        result, attempts = await complete_subscription_chain(
            step_req, cfg, session=session
        )
        for att in attempts:
            att = dict(att)
            att["tool_step"] = step
            all_attempts.append(att)
            budget = _remaining_budget(budget, att.get("latency_ms"))
        last = result
        if result.error is not None:
            return result, all_attempts
        if result.tool_calls:
            if step >= max_steps - 1:
                return CompletionResult(
                    provider=result.provider,
                    model=result.model,
                    error=CompletionError(
                        CompletionErrorCode.SCHEMA,
                        "tool step limit reached",
                        retryable=False,
                    ),
                    latency_ms=result.latency_ms,
                ), all_attempts
            # App-server resumes the same native turn. Grok retains the legacy
            # prompt-history loop until that provider is revisited.
            call_payload = [
                {"id": c.id, "name": c.name, "arguments": dict(c.arguments)}
                for c in result.tool_calls
            ]
            if not result.continuation_id:
                messages.append({
                    "role": "assistant",
                    "content": json_dumps_safe({"type": "tool_calls", "tool_calls": call_payload}),
                })
            returned_results: list[Mapping[str, Any]] = []
            for call in result.tool_calls:
                tool_started = time.monotonic()
                tool_timeout = _DEFAULT_TOOL_TIMEOUT_S
                if budget is not None:
                    tool_timeout = min(max(1, budget), _DEFAULT_TOOL_TIMEOUT_S)
                try:
                    tool_result = await asyncio.wait_for(
                        execute_tool(call.name, dict(call.arguments)),
                        timeout=tool_timeout,
                    )
                except asyncio.TimeoutError:
                    tool_result = f"ERROR: TimeoutError: tool exceeded {tool_timeout}s"
                except Exception as exc:
                    tool_result = f"ERROR: {type(exc).__name__}: {exc}"
                budget = _remaining_budget(
                    budget, (time.monotonic() - tool_started) * 1000.0,
                )
                if result.continuation_id:
                    returned_results.append({
                        "id": call.id,
                        "text": tool_result,
                        "success": not tool_result.startswith("ERROR:"),
                    })
                else:
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Tool result for {call.name} (id={call.id}):\n{tool_result}"
                        ),
                    })
            continuation_id = result.continuation_id
            pending_tool_results = tuple(returned_results)
            continue
        # Step 0 answered in prose without tools on a narrow surface — nudge once.
        if (
            step == 0
            and request.provider == "grok"
            and request.tools
            and not result.tool_calls
            and not nudged_step0
            and len(request.tools) <= 25
        ):
            nudged_step0 = True
            messages.append({
                "role": "user",
                "content": (
                    "Call a tool from TOOLS for this request — do not answer from memory. "
                    'Reply with {"type":"tool_calls","tool_calls":[...]} only.'
                ),
            })
            continue
        # Synthesis hop returned empty text after tool results — nudge once.
        if (
            step > 0
            and not (result.text or "").strip()
            and not nudged_synthesis
        ):
            nudged_synthesis = True
            messages.append({
                "role": "user",
                "content": (
                    "Give a concise final answer for the user based on the tool results above."
                ),
            })
            continue
        # Final text
        return result, all_attempts

    assert last is not None
    return last, all_attempts


def json_dumps_safe(obj: Any) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"), default=str)
