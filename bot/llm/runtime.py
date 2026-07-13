"""Runtime / container wiring (Phase 4.4 5c).

Owns _container, _init, _get_db. llm/* modules use this (or lazy) instead of
importing claude_service to break cycles.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from service_container import ServiceContainer

log = logging.getLogger(__name__)

_container: ServiceContainer | None = None


def init(container: ServiceContainer) -> None:
    global _container
    _container = container
    try:
        from .model_state import _init as _init_model_state
        _init_model_state(container)
    except Exception:
        log.exception("llm.runtime.init: model_state init failed")


def get_db():
    """Database module wired via ServiceContainer.db."""
    if _container is None or _container.db is None:
        raise RuntimeError("database not wired — call llm.runtime.init(container) with container.db set")
    return _container.db


def get_container() -> ServiceContainer | None:
    return _container
