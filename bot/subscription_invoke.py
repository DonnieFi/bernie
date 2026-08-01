"""Invoke a model for text (or typed parse) with subscription-aware routing.

Subscription models (Codex/Grok) always use ``complete_subscription_chain``.
Legacy models keep existing Anthropic / OpenRouter / LiteLLM / Ollama paths.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

log = logging.getLogger(__name__)


def surface_ollama_fallback(config: Mapping[str, Any], surface: str) -> str | None:
    """Surface-specific Ollama tier-3 selection when configured."""
    surface = (surface or "").strip().lower()
    if surface in ("eval", "judge"):
        ev = (config.get("eval") or {})
        model = ev.get("judge_ollama_fallback")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return None


async def complete_text(
    model: str,
    *,
    config: Mapping[str, Any],
    prompt: str,
    system: str | None = None,
    messages: list[dict] | None = None,
    surface: str = "worker",
    output_schema: Mapping[str, Any] | None = None,
    timeout_s: int | None = None,
) -> str | None:
    """Return final text or None on failure. Never uses llm_for for codex/grok."""
    from model_catalog import is_subscription_enabled
    from completion_router import CompletionRequest, subscription_model
    from subscription_complete import complete_subscription_chain, log_subscription_attempts

    if is_subscription_enabled(model, config):
        entry = subscription_model(model, config)
        assert entry is not None
        req = CompletionRequest(
            surface=surface,
            provider=entry.provider,
            model=model,
            system=system,
            prompt=prompt,
            messages=tuple(messages or ()),
            output_schema=output_schema,
            timeout_s=timeout_s,
        )
        result, attempts = await complete_subscription_chain(
            req,
            config,
            surface_ollama_model=surface_ollama_fallback(config, surface),
        )
        try:
            await log_subscription_attempts(
                attempts,
                user_input=prompt[:200] if prompt else "",
                final_text=(result.text or "")[:200],
                triggered_by=surface,
                surface=surface,
            )
        except Exception:
            log.debug("subscription invoke telemetry failed", exc_info=True)
        if result.error or not result.text:
            log.warning(
                "subscription complete_text failed model=%s err=%s",
                model,
                result.error.code.value if result.error else "empty",
            )
            return None
        return result.text

    # Legacy: prefer container clients when available
    try:
        from llm.runtime import get_container
        container = get_container()
    except Exception:
        container = None

    if container is not None:
        from completion_router import MissingProviderClient

        try:
            client = container.llm_for(model)
        except MissingProviderClient:
            client = None
        if isinstance(client, str):
            from llm.ollama import call_ollama
            return await call_ollama(
                system or "",
                messages or [{"role": "user", "content": prompt}],
                dict(config),
                None,
                model_override=model,
                user_message=prompt,
            )
        if client is not None:
            from llm.queue import queued_messages_create
            msgs = list(messages) if messages else [{"role": "user", "content": prompt}]
            resp = await queued_messages_create(
                client,
                dict(config),
                model=model,
                max_tokens=2048,
                system=system or "",
                messages=msgs,
            )
            return resp.content[0].text if resp.content else None

    # Last resort: Ollama topic helper
    try:
        from worker import _call_ollama_topic
        text, _ = await _call_ollama_topic(model, prompt, dict(config), system=system or "")
        return text
    except Exception:
        log.exception("complete_text legacy fallback failed model=%s", model)
        return None


async def complete_typed(
    model: str,
    result_type: type,
    *,
    config: Mapping[str, Any],
    prompt: str,
    system: str | None = None,
    surface: str = "worker",
    timeout_s: int | None = None,
):
    """Subscription → text + parse_typed; else make_typed_agent.run."""
    from model_catalog import is_subscription_enabled
    from agent_utils import parse_typed, make_typed_agent

    if is_subscription_enabled(model, config):
        schema = None
        if hasattr(result_type, "model_json_schema"):
            try:
                from json_schema_compat import sanitize_json_schema_for_structured_output

                schema = sanitize_json_schema_for_structured_output(
                    result_type.model_json_schema()
                )
            except Exception:
                schema = None
        text = await complete_text(
            model,
            config=config,
            prompt=prompt,
            system=system,
            surface=surface,
            output_schema=schema,
            timeout_s=timeout_s,
        )
        if not text:
            return None
        return parse_typed(text, result_type)

    agent = make_typed_agent(model, result_type)
    result = await agent.run(prompt if not system else f"{system}\n\n{prompt}")
    return result.output
