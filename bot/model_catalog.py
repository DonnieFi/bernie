"""Unified provider-aware model catalog for every selector surface.

Combines legacy Anthropic / LiteLLM / OpenRouter / Ollama pools with
``subscription_models`` (Codex/Grok). Selecting a model implies its provider.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from completion_router import (
    resolve_fallback_chain,
    subscription_catalog,
    subscription_model,
)

# Target → ALL capabilities required (exact set membership, no partial match).
# Research / study_guide / audit require structured-output (not tools): workers
# call models for JSON/markdown synthesis; tool use is orchestrated in Python.
TARGET_CAPABILITIES: dict[str, frozenset[str]] = {
    "discord": frozenset({"text", "tools"}),
    "active": frozenset({"text", "tools"}),
    "webui": frozenset({"text", "tools"}),
    "openwebui": frozenset({"text", "tools"}),
    "digest": frozenset({"text"}),
    "fallback": frozenset({"text", "ollama-only"}),
    "shadow": frozenset({"text"}),
    "worker": frozenset({"text"}),
    "research": frozenset({"text", "structured-output"}),
    "research_upgrade": frozenset({"text", "structured-output"}),
    "study_guide": frozenset({"text", "structured-output"}),
    "audit": frozenset({"text", "structured-output"}),
    "eval": frozenset({"text", "judge"}),
    "judge_fallback": frozenset({"text", "judge", "litellm-only"}),
    "judge_ollama": frozenset({"text", "judge", "ollama-only"}),
    "vision": frozenset({"vision", "ollama-only"}),
    "primary_reliable": frozenset({"text", "tools", "native-escalation"}),
    "reflection": frozenset({"text"}),
    "consolidation": frozenset({"text"}),
}

# Human-readable rejection for legacy API contracts.
TARGET_ERROR_HINT: dict[str, str] = {
    "fallback": "fallback target accepts Ollama models only",
    "vision": "vision target accepts Ollama models only",
    "judge_fallback": "judge_fallback accepts LiteLLM models only",
    "judge_ollama": "judge_ollama_fallback accepts Ollama models only",
    "primary_reliable": "primary_reliable target accepts Anthropic/LiteLLM models only",
}


@dataclass(frozen=True)
class CatalogEntry:
    model: str
    provider: str
    capabilities: frozenset[str]
    readiness: str
    enabled: bool
    fallback_chain: tuple[dict[str, str], ...]
    display: str


def _legacy_capabilities(provider: str) -> frozenset[str]:
    if provider == "ollama":
        # structured-output: study_guide / research / audit / reflection workers
        # historically use Ollama for JSON and markdown synthesis.
        return frozenset({"text", "ollama-only", "judge", "structured-output"})
    if provider in ("litellm", "openrouter"):
        return frozenset({
            "text", "tools", "litellm-only", "judge", "structured-output", "native-escalation",
        })
    # anthropic
    return frozenset({"text", "tools", "structured-output", "judge", "native-escalation"})


def _ollama_capabilities(model: str, cfg: Mapping[str, Any]) -> frozenset[str]:
    # All configured Ollama ids may be selected for vision (runtime uses
    # vision_model as the active choice). Restrict non-Ollama elsewhere.
    return _legacy_capabilities("ollama") | frozenset({"vision"})


def _subscription_chain(model: str, cfg: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    try:
        chain = resolve_fallback_chain(model, cfg)
        return tuple({"provider": t.provider, "model": t.model} for t in chain)
    except ValueError:
        entry = subscription_model(model, cfg)
        if entry is None:
            return ()
        return (
            {"provider": entry.provider, "model": entry.model},
            {"provider": "openrouter", "model": entry.openrouter_fallback_model},
        )


def provider_readiness_map(
    cfg: Mapping[str, Any],
    *,
    runner_health: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Map provider → readiness string (secret-safe)."""
    from completion_router import ollama_route_viable

    out = {
        "anthropic": "ready" if _env_set("ANTHROPIC_API_KEY") else "unavailable",
        "openrouter": "ready" if _env_set("OPENROUTER_API_KEY") or _env_set("OPENROUTER_API_KEY_LITE") else "unavailable",
        "litellm": "ready" if _env_set("LTE_LLM_MASTER_KEY") or _env_set("ANTHROPIC_API_KEY") else "unavailable",
        "ollama": "ready" if ollama_route_viable(cfg) else "unavailable",
        "codex": "unknown",
        "grok": "unknown",
    }
    providers = {}
    if isinstance(runner_health, Mapping):
        providers = runner_health.get("providers") or {}
    if isinstance(providers, Mapping):
        for key in ("codex", "grok"):
            val = providers.get(key)
            if val in ("ready", "unavailable", "reauth-required"):
                out[key] = str(val)
    return out


def _env_set(name: str) -> bool:
    import os
    return bool(os.environ.get(name, "").strip())


def build_catalog(
    cfg: Mapping[str, Any],
    *,
    runner_health: Mapping[str, Any] | None = None,
    include_disabled_subscription: bool = False,
    extra_litellm_ids: Iterable[str] | None = None,
) -> list[CatalogEntry]:
    """Full selector catalog: legacy pools + optional live LiteLLM ids + subscription."""
    readiness = provider_readiness_map(cfg, runner_health=runner_health)
    entries: list[CatalogEntry] = []

    for model in cfg.get("anthropic_models") or []:
        if not isinstance(model, str) or not model.strip():
            continue
        entries.append(CatalogEntry(
            model=model,
            provider="anthropic",
            capabilities=_legacy_capabilities("anthropic"),
            readiness=readiness.get("anthropic", "unknown"),
            enabled=True,
            fallback_chain=({"provider": "anthropic", "model": model},),
            display=f"{model} (anthropic)",
        ))

    litellm_ids: list[str] = []
    for model in cfg.get("litellm_models") or []:
        if isinstance(model, str) and model.strip():
            litellm_ids.append(model.strip())
    if extra_litellm_ids:
        for model in extra_litellm_ids:
            if isinstance(model, str) and model.strip():
                litellm_ids.append(model.strip())

    from openrouter_models import openrouter_direct_enabled
    seen_lite: set[str] = set()
    for model in litellm_ids:
        if model in seen_lite:
            continue
        seen_lite.add(model)
        provider = "openrouter" if openrouter_direct_enabled(cfg) and model.startswith("or-") else "litellm"
        # Live discovery ids are always litellm source for web UI contracts.
        source_provider = "litellm" if model not in (cfg.get("litellm_models") or []) else provider
        # Prefer litellm source for pool merge display when from live-only
        display_provider = "litellm" if source_provider == "litellm" or not openrouter_direct_enabled(cfg) else provider
        if model not in (cfg.get("litellm_models") or []):
            display_provider = "litellm"
            provider = "litellm"
        entries.append(CatalogEntry(
            model=model,
            provider=provider if model in (cfg.get("litellm_models") or []) else "litellm",
            capabilities=_legacy_capabilities("litellm"),
            readiness=readiness.get("litellm", "unknown"),
            enabled=True,
            fallback_chain=({"provider": "litellm", "model": model},),
            display=f"{model} ({display_provider})",
        ))

    for model in cfg.get("ollama_models") or []:
        if not isinstance(model, str) or not model.strip():
            continue
        entries.append(CatalogEntry(
            model=model,
            provider="ollama",
            capabilities=_ollama_capabilities(model, cfg),
            readiness=readiness.get("ollama", "unknown"),
            enabled=True,
            fallback_chain=({"provider": "ollama", "model": model},),
            display=f"{model} (ollama)",
        ))

    for sub in subscription_catalog(cfg):
        if not sub.enabled and not include_disabled_subscription:
            continue
        chain = _subscription_chain(sub.model, cfg) if sub.enabled else (
            {"provider": sub.provider, "model": sub.model},
        )
        entries.append(CatalogEntry(
            model=sub.model,
            provider=sub.provider,
            capabilities=sub.capabilities,
            readiness=(
                "disabled" if not sub.enabled
                else readiness.get(sub.provider, "unknown")
            ),
            enabled=sub.enabled,
            fallback_chain=chain,
            display=f"{sub.model} ({sub.provider})",
        ))

    by_id: dict[str, CatalogEntry] = {}
    for entry in entries:
        if entry.model in by_id and by_id[entry.model].provider in ("codex", "grok"):
            continue
        if entry.provider in ("codex", "grok"):
            by_id[entry.model] = entry
        elif entry.model not in by_id:
            by_id[entry.model] = entry
    return list(by_id.values())


def filter_for_target(
    entries: Iterable[CatalogEntry],
    target: str,
) -> list[CatalogEntry]:
    """Keep entries that have EVERY capability required by ``target``."""
    required = TARGET_CAPABILITIES.get(target, frozenset({"text"}))
    out: list[CatalogEntry] = []
    for entry in entries:
        if not entry.enabled:
            continue
        if required <= entry.capabilities:
            out.append(entry)
    return out


def filter_for_active_auth(
    entries: Iterable[CatalogEntry],
    active_model: str,
) -> list[CatalogEntry]:
    """For subscription auth, show only models using that direct provider."""
    entries = list(entries)
    active = next((entry for entry in entries if entry.model == active_model), None)
    if active is None or active.provider not in ("codex", "grok"):
        return entries
    return [entry for entry in entries if entry.provider == active.provider]


def catalog_as_dicts(
    cfg: Mapping[str, Any],
    *,
    target: str | None = None,
    runner_health: Mapping[str, Any] | None = None,
    extra_litellm_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    entries = build_catalog(
        cfg, runner_health=runner_health, extra_litellm_ids=extra_litellm_ids
    )
    if target:
        entries = filter_for_target(entries, target)
    return [
        {
            "id": e.model,
            "model": e.model,
            "provider": e.provider,
            "source": e.provider,
            "capabilities": sorted(e.capabilities),
            "readiness": e.readiness,
            "enabled": e.enabled,
            "fallback_chain": list(e.fallback_chain),
            "display": e.display,
        }
        for e in entries
    ]


def validate_model_for_target(model_id: str, target: str, cfg: Mapping[str, Any]) -> str | None:
    """Return error detail or None if ok. Error text preserves legacy contracts."""
    # Normalize discord → active
    if target == "discord":
        target = "active"

    all_entries = build_catalog(cfg)
    all_ids = {e.model for e in all_entries}
    if model_id not in all_ids:
        return f"unknown model: {model_id}"

    allowed = {e.model for e in filter_for_target(all_entries, target)}
    if model_id in allowed:
        return None

    # Legacy provider-specific messages
    if target in TARGET_ERROR_HINT:
        return TARGET_ERROR_HINT[target]
    return f"model not allowed for target {target}"


def format_chain(entry: CatalogEntry | None, model: str, cfg: Mapping[str, Any]) -> str:
    if entry is None:
        sub = subscription_model(model, cfg)
        if sub and sub.enabled:
            try:
                chain = resolve_fallback_chain(model, cfg)
                return " → ".join(f"{t.provider}/{t.model}" for t in chain)
            except ValueError:
                return f"{sub.provider}/{model}"
        from model_registry import model_source
        return f"{model_source(model, cfg)}/{model}"
    return " → ".join(f"{h['provider']}/{h['model']}" for h in entry.fallback_chain)


def is_subscription_enabled(model: str | None, cfg: Mapping[str, Any]) -> bool:
    entry = subscription_model(model, cfg)
    return entry is not None and entry.enabled
