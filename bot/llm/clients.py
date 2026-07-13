"""LLM client factory and lifecycle helpers (Phase 4.4 Session 1).

Moved from claude_service.py. All client *routing* stays in
ServiceContainer.llm_for(model) — this module only handles:
  - AsyncAnthropic client construction (_make_client / make_client)
  - Ephemeral vs singleton lifecycle detection + cleanup
  - Model cache-support info string

Does NOT import claude_service — uses an injected ``container`` parameter
where singleton checks are needed.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from anthropic import AsyncAnthropic

if TYPE_CHECKING:
    from service_container import ServiceContainer

log = logging.getLogger(__name__)


def make_openrouter_client(api_key: str | None = None) -> AsyncAnthropic:
    """AsyncAnthropic pointed at OpenRouter's Anthropic-compatible gateway."""
    resolved = (
        api_key
        or os.environ.get("OPENROUTER_API_KEY", "").strip()
        or os.environ.get("OPENROUTER_API_KEY_LITE", "").strip()
    )
    if not resolved:
        raise RuntimeError("OPENROUTER_API_KEY (or OPENROUTER_API_KEY_LITE) is not set.")

    import httpx

    http_client = httpx.AsyncClient(timeout=httpx.Timeout(90.0))
    client = AsyncAnthropic(
        base_url="https://openrouter.ai/api",
        api_key=resolved,
        default_headers={
            "HTTP-Referer": "https://bernie.lan",
            "X-Title": "Bernie Family Bot",
        },
        http_client=http_client,
    )
    client._owned_http_client = http_client
    return client


def make_client(base_url: str | None = None, api_key: str | None = None) -> AsyncAnthropic:
    """Build an AsyncAnthropic client (direct or via LiteLLM proxy).

    When ``base_url`` is provided the client talks to that proxy; otherwise
    it goes direct to api.anthropic.com.
    """
    resolved_api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not resolved_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set.")

    if base_url:
        import httpx
        # LiteLLM/Ollama path — enable SSL verification.
        # If calling local litellm.example.local and we have the mounted Caddy CA, use it.
        verify: str | bool = True
        if "litellm.example.local" in base_url and os.path.exists("/app/caddy-root.crt"):
            verify = "/app/caddy-root.crt"

        http_client = httpx.AsyncClient(verify=verify)
        client = AsyncAnthropic(
            base_url=base_url,
            api_key=os.environ.get("LTE_LLM_MASTER_KEY", resolved_api_key),
            default_headers={"x-litellm-api-key": os.environ.get("LTE_LLM_MASTER_KEY", resolved_api_key)},
            http_client=http_client,
        )
        client._owned_http_client = http_client
        return client
    return AsyncAnthropic(api_key=resolved_api_key)


def make_observed_anthropic_client(api_key: str | None = None) -> AsyncAnthropic:
    """Public alias used by nightly_digest and other modules."""
    return make_client(api_key=api_key)


def llm_client_is_ephemeral(
    client: Any,
    container: "ServiceContainer | None" = None,
) -> bool:
    """True when the caller owns this client and must close it after use.

    Container singletons (anthropic / litellm from ServiceContainer.llm_for)
    are process-wide and must not be closed per call.
    """
    if client is None:
        return False
    if container is not None:
        if client is container.anthropic or client is container.litellm or client is container.openrouter:
            return False
    return True


async def close_client(
    client: AsyncAnthropic | None,
    container: "ServiceContainer | None" = None,
) -> None:
    """Close an ephemeral AsyncAnthropic client and any owned httpx client."""
    if not llm_client_is_ephemeral(client, container):
        return
    try:
        await client.close()
    finally:
        owned = getattr(client, '_owned_http_client', None)
        if owned is not None:
            await owned.aclose()


def model_cache_support(model: str) -> str:
    """One-line note describing prompt-caching behaviour for a given model.

    Prefers explicit ``cache_mode`` stored in LiteLLM model_info (from the
    improved add flow), then falls back to the manual ``caching_auto_or_models``
    list in config.json.
    """
    if model.startswith("claude-"):
        return "✅ Prompt caching active (Anthropic cache_control; ≥1024 tokens to engage)."

    # Fast lookup from in-memory cache (populated on add/remove/reload)
    try:
        from litellm_service import get_model_cache_mode
        stored = get_model_cache_mode(model)
        if stored == "auto_provider":
            return ("⚠️ Provider auto-caches server-side (stored on registration). "
                    "LiteLLM drops cache-hit fields; savings are real but invisible here.")
        if stored == "anthropic":
            return "✅ Prompt caching active (we send cache_control for this model)."
        if stored == "none":
            return "❌ No prompt caching (explicitly set on registration)."
    except Exception:
        pass

    # Fallback to the manual list in config
    try:
        from config import config as _cfg
        auto = set(_cfg.get("caching_auto_or_models", []) or [])
    except Exception:
        auto = set()
    if model in auto:
        return ("⚠️ Provider auto-caches server-side, but LiteLLM's /v1/messages "
                "endpoint drops the cache-hit fields. Savings happen upstream; "
                "the dashboard will show 0 cache tokens.")
    return "❌ No prompt caching for this model — full input price every turn."
