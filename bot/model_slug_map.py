"""Unified Codex (canonical) ↔ OpenRouter ↔ LiteLLM slug mapping.

``subscription_models`` is the source of truth for subscription-backed models.
Each entry defines:
  - ``model`` — Codex/subscription canonical id (e.g. ``gpt-5.4``)
  - ``openrouter_fallback_model`` — OpenRouter slug (e.g. ``openai/gpt-5.4``)
  - ``litellm_alias`` (optional) — Bernie LiteLLM proxy name (e.g. ``or-gpt-5-4``)

When ``litellm_alias`` is omitted, the default is ``or-`` + canonical with ``.`` → ``-``.
Legacy aliases in ``_LEGACY_LITELLM`` keep existing config working.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

log = logging.getLogger(__name__)

# Existing production aliases → canonical subscription id.
_LEGACY_LITELLM_TO_CANONICAL: dict[str, str] = {
    "or-gpt54-mini": "gpt-5.4-mini",
    "or-gpt-5-4-mini": "gpt-5.4-mini",
    "or-gpt-56-luna": "gpt-5.6-luna",
    "or-gpt-56-sol": "gpt-5.6-sol",
    "or-gpt-56-terra": "gpt-5.6-terra",
}


@dataclass(frozen=True)
class SlugTriple:
    canonical: str
    openrouter: str
    litellm: str


def default_litellm_alias(canonical: str) -> str:
    """Derive Bernie ``or-*`` alias from a Codex canonical id."""
    base = canonical.strip()
    if base.startswith("or-"):
        return base
    return f"or-{base.replace('.', '-')}"


def _norm_key(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", model.lower())


@lru_cache(maxsize=8)
def _triples_from_cfg(cfg_key: tuple[tuple[str, str, str, str], ...]) -> tuple[SlugTriple, ...]:
    """Build slug triples from a hashable config snapshot."""
    triples: list[SlugTriple] = []
    seen_canonical: set[str] = set()
    for provider, canonical, openrouter, litellm in cfg_key:
        if not canonical or canonical in seen_canonical:
            continue
        seen_canonical.add(canonical)
        litellm_alias = litellm or default_litellm_alias(canonical)
        triples.append(SlugTriple(canonical, openrouter, litellm_alias))
        if provider == "grok" and canonical == "grok-4.5":
            triples.append(SlugTriple("grok-4.5", openrouter, litellm_alias))
    return tuple(triples)


def _cfg_snapshot(cfg: Mapping[str, Any] | None) -> tuple[tuple[str, str, str, str], ...]:
    rows: list[tuple[str, str, str, str]] = []
    for raw in (cfg or {}).get("subscription_models") or []:
        if not isinstance(raw, Mapping):
            continue
        provider = str(raw.get("provider") or "")
        canonical = str(raw.get("model") or "").strip()
        openrouter = str(raw.get("openrouter_fallback_model") or "").strip()
        litellm = str(raw.get("litellm_alias") or "").strip()
        if canonical and openrouter:
            rows.append((provider, canonical, openrouter, litellm))
    return tuple(sorted(rows))


def slug_triples(cfg: Mapping[str, Any] | None = None) -> tuple[SlugTriple, ...]:
    return _triples_from_cfg(_cfg_snapshot(cfg))


def _lookup_maps(cfg: Mapping[str, Any] | None) -> tuple[dict[str, SlugTriple], dict[str, str]]:
    """Return (any_key → triple, any_key → openrouter slug)."""
    by_key: dict[str, SlugTriple] = {}
    to_openrouter: dict[str, str] = {}

    def _register(key: str, triple: SlugTriple) -> None:
        if not key:
            return
        by_key[key] = triple
        by_key[key.lower()] = triple
        by_key[_norm_key(key)] = triple
        to_openrouter[key] = triple.openrouter
        to_openrouter[key.lower()] = triple.openrouter
        to_openrouter[_norm_key(key)] = triple.openrouter

    for triple in slug_triples(cfg):
        _register(triple.canonical, triple)
        _register(triple.openrouter, triple)
        _register(triple.litellm, triple)

    for legacy, canonical in _LEGACY_LITELLM_TO_CANONICAL.items():
        match = next((t for t in slug_triples(cfg) if t.canonical == canonical), None)
        if match:
            _register(legacy, match)

    return by_key, to_openrouter


def resolve_canonical(model: str | None, cfg: Mapping[str, Any] | None = None) -> str | None:
    if not model:
        return None
    by_key, _ = _lookup_maps(cfg)
    if model in by_key:
        return by_key[model].canonical
    low = model.lower()
    if low in by_key:
        return by_key[low].canonical
    norm = _norm_key(model)
    if norm in by_key:
        return by_key[norm].canonical
    if model in _LEGACY_LITELLM_TO_CANONICAL:
        return _LEGACY_LITELLM_TO_CANONICAL[model]
    if low in _LEGACY_LITELLM_TO_CANONICAL:
        return _LEGACY_LITELLM_TO_CANONICAL[low]
    if "/" not in model:
        return model
    return None


def resolve_litellm_alias(model: str | None, cfg: Mapping[str, Any] | None = None) -> str | None:
    if not model:
        return None
    by_key, _ = _lookup_maps(cfg)
    for key in (model, model.lower(), _norm_key(model)):
        triple = by_key.get(key)
        if triple:
            return triple.litellm
    canonical = resolve_canonical(model, cfg)
    if canonical:
        return default_litellm_alias(canonical)
    if model.startswith("or-"):
        return model
    return None


def resolve_openrouter(model: str | None, cfg: Mapping[str, Any] | None = None) -> str | None:
    """Map any Bernie/Codex/LiteLLM id to an OpenRouter slug, or None if unknown."""
    if not model:
        return None
    if "/" in model:
        return model
    _, to_openrouter = _lookup_maps(cfg)
    for key in (model, model.lower(), _norm_key(model)):
        slug = to_openrouter.get(key)
        if slug:
            return slug
    return None


def openrouter_alias_table(cfg: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Flat alias→openrouter map for merging into openrouter_models."""
    _, to_openrouter = _lookup_maps(cfg)
    # Prefer original casing keys for stable merges.
    out: dict[str, str] = {}
    for triple in slug_triples(cfg):
        for key in (triple.canonical, triple.litellm, triple.openrouter):
            out[key] = triple.openrouter
            out[key.lower()] = triple.openrouter
    for legacy in _LEGACY_LITELLM_TO_CANONICAL:
        slug = resolve_openrouter(legacy, cfg)
        if slug:
            out[legacy] = slug
            out[legacy.lower()] = slug
    out.update(to_openrouter)
    return out


def validate_subscription_slug_alignment(cfg: Mapping[str, Any] | None = None) -> list[str]:
    """Return human-readable warnings for misaligned litellm_models vs subscription map."""
    cfg = cfg or {}
    warnings: list[str] = []
    litellm_models = [m for m in (cfg.get("litellm_models") or []) if isinstance(m, str)]

    for raw in (cfg.get("subscription_models") or []):
        if not isinstance(raw, Mapping):
            continue
        canonical = str(raw.get("model") or "").strip()
        openrouter = str(raw.get("openrouter_fallback_model") or "").strip()
        if not canonical or not openrouter:
            continue
        if not openrouter.startswith(("openai/", "x-ai/", "anthropic/", "google/")) and "/" not in openrouter:
            warnings.append(
                f"subscription {canonical}: openrouter_fallback_model {openrouter!r} "
                "does not look like an OpenRouter slug (expected vendor/model)"
            )

    for litellm_id in litellm_models:
        if not litellm_id.startswith("or-"):
            continue
        slug = resolve_openrouter(litellm_id, cfg)
        if not slug:
            warnings.append(f"litellm_models entry {litellm_id!r} has no OpenRouter slug mapping")

    return warnings


def invalidate_slug_cache() -> None:
    _triples_from_cfg.cache_clear()
