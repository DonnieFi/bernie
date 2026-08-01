"""Provider-neutral completion contract and deterministic fallback planning.

Adapters execute elsewhere.  This module is deliberately pure so chat, workers,
tests, and the future subscription runner all share one routing seam.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping

Provider = Literal["anthropic", "litellm", "openrouter", "ollama", "codex", "grok"]
SubscriptionProvider = Literal["codex", "grok"]


class ProviderReadiness(str, Enum):
    UNKNOWN = "unknown"
    DISABLED = "disabled"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    REAUTH_REQUIRED = "reauth-required"


class CompletionErrorCode(str, Enum):
    TIMEOUT = "timeout"
    AUTH = "auth"
    QUOTA = "quota"
    UNAVAILABLE = "unavailable"
    BUSY = "busy"
    UPSTREAM = "upstream"
    SCHEMA = "schema"
    INVALID_REQUEST = "invalid-request"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class CompletionError:
    code: CompletionErrorCode
    message: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class CompletionRequest:
    surface: str
    provider: Provider
    model: str
    messages: tuple[Mapping[str, Any], ...] = ()
    system: str | None = None
    prompt: str | None = None
    tools: tuple[Mapping[str, Any], ...] = ()
    output_schema: Mapping[str, Any] | None = None
    timeout_s: int | None = None
    continuation_id: str | None = None
    tool_results: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class CompletionResult:
    provider: Provider
    model: str
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    error: CompletionError | None = None
    continuation_id: str | None = None


@dataclass(frozen=True)
class ModelCatalogEntry:
    model: str
    provider: SubscriptionProvider
    capabilities: frozenset[str]
    openrouter_fallback_model: str
    enabled: bool
    readiness: ProviderReadiness


@dataclass(frozen=True)
class RouteTarget:
    provider: Provider
    model: str


def subscription_catalog(cfg: Mapping[str, Any]) -> tuple[ModelCatalogEntry, ...]:
    """Return configured subscription models in stable config order."""
    entries: list[ModelCatalogEntry] = []
    for raw in cfg.get("subscription_models") or []:
        if not isinstance(raw, Mapping):
            raise ValueError("subscription_models entries must be objects")
        provider = raw.get("provider")
        model = raw.get("model")
        fallback = raw.get("openrouter_fallback_model")
        capabilities = raw.get("capabilities")
        if provider not in ("codex", "grok"):
            raise ValueError(f"invalid subscription provider for {model!r}: {provider!r}")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("subscription model id must be non-empty")
        if not isinstance(fallback, str) or not fallback.strip():
            raise ValueError(f"subscription model {model!r} requires openrouter_fallback_model")
        if not isinstance(capabilities, (list, tuple, set)) or not capabilities:
            raise ValueError(f"subscription model {model!r} requires capabilities")
        enabled = bool(raw.get("enabled", False))
        entries.append(ModelCatalogEntry(
            model=model,
            provider=provider,
            capabilities=frozenset(str(value) for value in capabilities),
            openrouter_fallback_model=fallback,
            enabled=enabled,
            readiness=ProviderReadiness.UNKNOWN if enabled else ProviderReadiness.DISABLED,
        ))
    return tuple(entries)


def subscription_model(model: str | None, cfg: Mapping[str, Any]) -> ModelCatalogEntry | None:
    """Look up one subscription model without classifying legacy models."""
    if not model:
        return None
    canonical = model
    try:
        from model_slug_map import resolve_canonical

        resolved = resolve_canonical(model, cfg)
        if resolved:
            canonical = resolved
    except Exception:
        pass
    return next((entry for entry in subscription_catalog(cfg) if entry.model == canonical), None)


class MissingProviderClient(Exception):
    """Selected provider client is not constructed (missing credentials).

    Callers should treat this as a typed *retryable* failure and advance the
    configured fallback chain rather than AttributeError on ``None``.
    """

    def __init__(self, provider: str, model: str, message: str = ""):
        self.provider = provider
        self.model = model
        self.retryable = True
        self.code = CompletionErrorCode.UNAVAILABLE
        super().__init__(message or f"{provider} client unavailable for model {model!r}")

    def as_completion_error(self) -> CompletionError:
        return CompletionError(
            code=self.code,
            message=str(self),
            retryable=True,
        )


def _env_flag(env: Mapping[str, str], *names: str) -> bool:
    return any(bool((env.get(n) or "").strip()) for n in names)


def _ollama_fallback_model(
    cfg: Mapping[str, Any],
    *,
    surface_ollama_model: str | None = None,
) -> str | None:
    """Selected Ollama id when it is in ``ollama_models``, else None."""
    ollama_models = cfg.get("ollama_models") or []
    candidate = surface_ollama_model or (cfg.get("llm_fallback") or {}).get("model")
    if isinstance(candidate, str) and candidate.strip() and candidate in ollama_models:
        return candidate
    return None


def ollama_route_viable(cfg: Mapping[str, Any]) -> bool:
    """True when Ollama models are configured and base URL is not a placeholder."""
    if not cfg.get("ollama_models"):
        return False
    from ollama_resolver import configured_ollama_base_url

    return configured_ollama_base_url(dict(cfg)) is not None


def subscription_route_viable(
    entry: ModelCatalogEntry,
    cfg: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> bool:
    """True when an enabled subscription entry has at least one usable fallback.

    Requires pinned ``openrouter_fallback_model`` plus **either** an OpenRouter
    API key **or** a configured Ollama fallback in ``ollama_models``.
    """
    if not entry.enabled:
        return False
    if not (entry.openrouter_fallback_model or "").strip():
        return False
    import os
    e = env if env is not None else os.environ
    has_openrouter = _env_flag(e, "OPENROUTER_API_KEY", "OPENROUTER_API_KEY_LITE")
    has_ollama = _ollama_fallback_model(cfg) is not None and ollama_route_viable(cfg)
    return has_openrouter or has_ollama


def has_usable_llm_route(
    cfg: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    *,
    runner_health: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """Production startup gate: at least one usable configured LLM route.

    Returns ``(ok, reason)``. Reasons are secret-free labels for logs/tests.

    Order of preference for the reason label (first match wins):
      anthropic → openrouter → viable subscription → ollama-only

    Subscription entries that are disabled, missing OpenRouter mapping, or
    lack any viable fallback do **not** count. Runner readiness of
    reauth-required / unavailable does not block startup when a fallback
    tier is configured (chain can still advance).
    """
    import os
    e = env if env is not None else os.environ

    if _env_flag(e, "ANTHROPIC_API_KEY"):
        return True, "anthropic"

    if _env_flag(e, "OPENROUTER_API_KEY", "OPENROUTER_API_KEY_LITE"):
        return True, "openrouter"

    # Subscription: need enabled + mapping + viable fallback (not bare enabled).
    try:
        for entry in subscription_catalog(cfg):
            if subscription_route_viable(entry, cfg, e):
                # Optional: note runner health in reason without blocking.
                readiness = "unknown"
                if isinstance(runner_health, Mapping):
                    providers = runner_health.get("providers") or {}
                    if isinstance(providers, Mapping):
                        readiness = str(providers.get(entry.provider) or "unknown")
                return True, f"subscription:{entry.provider}:{readiness}"
    except ValueError:
        # Malformed subscription_models — ignore for gate; ollama may still save us.
        pass

    if ollama_route_viable(cfg):
        return True, "ollama"

    return False, "none"


def resolve_fallback_chain(
    model: str,
    cfg: Mapping[str, Any],
    *,
    surface_ollama_model: str | None = None,
) -> tuple[RouteTarget, ...]:
    """Resolve subscription primary → optional OpenRouter → optional Ollama.

    At least one fallback tier (OpenRouter mapping and/or selected Ollama) is
    required.  The surface-specific Ollama selection wins over global
    ``llm_fallback.model``.  Host selection remains ``ollama_resolver``'s job.
    """
    entry = subscription_model(model, cfg)
    if entry is None:
        raise ValueError(f"unknown subscription model: {model}")
    if not entry.enabled:
        raise ValueError(f"subscription model is disabled: {model}")

    chain: list[RouteTarget] = [RouteTarget(entry.provider, entry.model)]
    or_model = (entry.openrouter_fallback_model or "").strip()
    if or_model:
        chain.append(RouteTarget("openrouter", or_model))
    ollama_model = _ollama_fallback_model(cfg, surface_ollama_model=surface_ollama_model)
    if ollama_model:
        chain.append(RouteTarget("ollama", ollama_model))
    if len(chain) < 2:
        raise ValueError(
            f"subscription model {model} needs openrouter_fallback_model "
            f"and/or a selected Ollama fallback in ollama_models"
        )
    return tuple(chain)
