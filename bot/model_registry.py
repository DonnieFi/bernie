"""Config-backed model classification helpers.

Explicit config pools are authoritative. Prefixes are only a fallback for
unregistered model names, which lets a `claude-*` alias route through LiteLLM
when it is deliberately listed there.
"""

from __future__ import annotations

from typing import Literal

ModelSource = Literal["anthropic", "litellm", "ollama", "openrouter"]

# Canonical default when config has no active_model (see model_state.py).
DEFAULT_MODEL = "claude-sonnet-5"


def model_source(model: str | None, cfg: dict) -> ModelSource:
    """Return the configured source for a model, using prefixes only last."""
    from openrouter_models import openrouter_direct_enabled

    if model in (cfg.get("ollama_models") or []):
        return "ollama"
    if model in (cfg.get("anthropic_models") or []):
        return "anthropic"
    if model in (cfg.get("litellm_models") or []):
        return "openrouter" if openrouter_direct_enabled(cfg) else "litellm"
    if model and model.startswith("claude-"):
        return "anthropic"
    if model and model.startswith("or-") and openrouter_direct_enabled(cfg):
        return "openrouter"
    return "openrouter" if openrouter_direct_enabled(cfg) else "litellm"


def model_base_url(model: str | None, cfg: dict) -> str | None:
    """Return the base URL implied by the model's configured source."""
    from openrouter_models import OPENROUTER_API_BASE

    source = model_source(model, cfg)
    if source == "ollama":
        return cfg.get("ollama_base_url", "http://192.168.1.X:11434")  # placeholder; set in config.json
    if source == "openrouter":
        return OPENROUTER_API_BASE
    if source == "litellm":
        return cfg.get("litellm_base_url", "https://litellm.example.local")
    return None


def active_model_from_config(cfg: dict, fallback: str) -> str:
    """Current chat model, with a config-driven default before legacy fallback."""
    return (
        cfg.get("active_model")
        or cfg.get("default_chat_model")
        or cfg.get("default_model")
        or fallback
    )


def reset_model_from_config(cfg: dict, fallback: str) -> str:
    """Model used by admin reset commands."""
    return (
        cfg.get("default_chat_model")
        or cfg.get("active_model")
        or cfg.get("default_model")
        or fallback
    )
