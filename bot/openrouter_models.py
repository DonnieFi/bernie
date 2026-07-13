"""OpenRouter model alias resolution and API key lookup."""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

OPENROUTER_API_BASE = "https://openrouter.ai/api"


def openrouter_direct_enabled(cfg: dict) -> bool:
    return bool(cfg.get("openrouter_direct", True))


def openrouter_api_key(cfg: dict | None = None) -> str:
    cfg = cfg or {}
    for entry in cfg.get("openrouter_keys") or [
        {"env": "OPENROUTER_API_KEY"},
        {"env": "OPENROUTER_API_KEY_LITE"},
    ]:
        env_name = entry.get("env", "")
        if not env_name:
            continue
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return (
        os.environ.get("OPENROUTER_API_KEY", "").strip()
        or os.environ.get("OPENROUTER_API_KEY_LITE", "").strip()
    )


def _prices_path() -> Path:
    return Path(__file__).with_name("model_prices.json")


@lru_cache(maxsize=1)
def _alias_table() -> dict[str, str]:
    aliases: dict[str, str] = {}
    try:
        with _prices_path().open(encoding="utf-8") as f:
            data = json.load(f)
        for alias, slug in (data.get("_litellm_aliases") or {}).items():
            aliases[alias] = slug
            aliases[alias.lower()] = slug
    except Exception:
        log.warning("openrouter_models: could not load model_prices aliases", exc_info=True)

    # Common Bernie aliases not yet in the JSON table.
    extras = {
        "or-gpt54-mini": "openai/gpt-5.4-mini",
        "or-gpt-5-4-mini": "openai/gpt-5.4-mini",
        "or-deepseek-v4-pro": "deepseek/deepseek-v4-flash",
        "or-grok-45": "x-ai/grok-4.5",
        "or-grok-build": "x-ai/grok-code-fast-1",
        "or-op-minimax-m3": "minimax/minimax-m2.5",
        "or-cohere-north": "cohere/command-a",
        "or-kimi-27-code": "moonshotai/kimi-k2.5",
        "or-mistral-small4": "mistralai/mistral-small-3.2-24b-instruct",
        "or-mimo-25-pro": "xiaomi/mimo-v2.5-pro",
        "or-mimo-pro": "xiaomi/mimo-v2.5-pro",
    }
    for alias, slug in extras.items():
        aliases.setdefault(alias, slug)
        aliases.setdefault(alias.lower(), slug)
    return aliases


def invalidate_alias_table() -> None:
    """Drop cached alias→slug map (after model-add /reload)."""
    _alias_table.cache_clear()


def register_openrouter_alias(alias: str, openrouter_slug: str) -> None:
    """Persist alias→OpenRouter slug for openrouter_direct routing.

    Writes model_prices.json `_litellm_aliases` and clears the in-process cache.
    LiteLLM registration alone is not enough when openrouter_direct=true.
    """
    if not alias or not openrouter_slug:
        return
    if not alias.startswith("or-"):
        alias = f"or-{alias}"

    path = _prices_path()
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"_litellm_aliases": {}, "models": []}

    aliases = dict(data.get("_litellm_aliases") or {})
    aliases[alias] = openrouter_slug
    data["_litellm_aliases"] = aliases

    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)

    invalidate_alias_table()
    try:
        from database import reload_model_prices

        reload_model_prices()
    except Exception:
        log.debug("register_openrouter_alias: reload_model_prices skipped", exc_info=True)
    log.info("openrouter_models: registered %s → %s", alias, openrouter_slug)


def _fragment_slug(model: str) -> str | None:
    """Best-effort slug from model_prices fragment index."""
    try:
        with _prices_path().open(encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    needle = re.sub(r"[^a-z0-9]+", "", model.lower())
    if not needle:
        return None

    best: tuple[int, str] | None = None
    for entry in data.get("models") or []:
        or_id = entry.get("or_id")
        if not or_id:
            continue
        for frag in entry.get("fragments") or []:
            frag_norm = re.sub(r"[^a-z0-9]+", "", str(frag).lower())
            if not frag_norm:
                continue
            if frag_norm in needle or needle in frag_norm:
                score = len(frag_norm)
                if best is None or score > best[0]:
                    best = (score, or_id)
    return best[1] if best else None


def resolve_openrouter_slug(model: str | None, cfg: dict | None = None) -> str:
    """Map Bernie alias (or-gpt54-mini) to OpenRouter slug (openai/gpt-5.4-mini)."""
    if not model:
        return model or ""
    if "/" in model:
        return model

    aliases = _alias_table()
    if model in aliases:
        return aliases[model]
    low = model.lower()
    if low in aliases:
        return aliases[low]

    slug = _fragment_slug(model)
    if slug:
        return slug

    log.warning("openrouter_models: no slug mapping for %r — passing through", model)
    return model
