"""Model state management (Phase 4.4 Session 4).

Moved from claude_service.py: set_model, get_model_info, _base_url_for_model.
Preserves exact active_model / base_url behavior, config-driven, no new hardcoded routing.
claude_service re-exports the functions (and DEFAULT_MODEL for compat) for zero churn.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from service_container import ServiceContainer

# Model constants — canonical definition in model_registry.py
from model_registry import DEFAULT_MODEL

from config import config as _cfg
from model_registry import active_model_from_config, model_base_url

_container: ServiceContainer | None = None

_model: str = active_model_from_config(_cfg, DEFAULT_MODEL)
_base_url: str | None = model_base_url(_model, _cfg)


def _init(container: ServiceContainer | None = None) -> None:
    """Init for model state (delegated from facade _init for container)."""
    global _container
    _container = container


def set_model(model: str, base_url: str | None = None):
    global _model, _base_url
    _model = model or DEFAULT_MODEL
    
    if base_url is not None:
        _base_url = base_url
    else:
        _base_url = model_base_url(_model, _cfg)


def get_model_info() -> tuple[str, str | None]:
    global _model, _base_url
    from config import check_and_reload_config_if_modified
    
    config_modified = check_and_reload_config_if_modified()
    active_in_cfg = active_model_from_config(_cfg, DEFAULT_MODEL)
    
    if config_modified or _model != active_in_cfg:
        _model = active_in_cfg
        _base_url = model_base_url(_model, _cfg)
            
    return _model, _base_url


def _base_url_for_model(model_name: str) -> str | None:
    """Resolve proxy base URL for shadow/eval paths that still pass base_url through."""
    if _container:
        client_or_url = _container.llm_for(model_name)
        if isinstance(client_or_url, str):
            return client_or_url
        base = getattr(client_or_url, "base_url", None)
        if base and "api.anthropic.com" not in str(base):
            return str(base).rstrip("/")
        return None
    _, url = get_model_info()
    return url


def get_container() -> ServiceContainer | None:
    """For internal use by other llm modules if needed."""
    return _container
