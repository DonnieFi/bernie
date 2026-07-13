"""ServiceContainer — process-wide singleton holding every service instance.

Constructed once in main.py; injected into bot.py, api.py, and worker.py.
This is the supply side; ServiceRefs (executor.py) is the per-turn demand side.
Bridge: build_service_refs(container) in llm/services.py.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiohttp
    from calendar_service import CalendarService
    from api import ConnectionManager
    from notification_router import NotificationRouter
    from supervisor import TaskSupervisor
    from worker import CognitiveWorker, WatchdogWorker
    from ha_service import HAService
    from identity_service import IdentityService
    from network_service import NetworkService
    from presence_service import PresenceService
    from frigate_service import FrigateService

log = logging.getLogger(__name__)


@dataclass
class ServiceContainer:
    """Process-wide service registry.  Every field is populated by main.py
    before the event loop starts accepting requests."""

    # ── Stateful instances (constructed in main.py) ────────────────────────
    calendar: Any = None                   # CalendarService
    connection_manager: Any = None         # ConnectionManager
    notification_orchestrator: Any = None  # NotificationRouter
    supervisor: Any = None                 # TaskSupervisor
    scheduler: Any = None                  # BackgroundTaskScheduler (Wave 2a)
    cognitive_worker: Any = None           # CognitiveWorker
    watchdog_worker: Any = None            # WatchdogWorker
    session: Any = None                    # aiohttp.ClientSession

    # ── Module-level singletons (constructed in their own modules) ─────────
    frigate: Any = None   # FrigateService
    ha: Any = None        # HAService
    identity: Any = None  # IdentityService
    network: Any = None   # NetworkService
    presence: Any = None  # PresenceService

    # ── LLM inference clients (constructed in main.py) ────────────────────
    # Three peers; routing via llm_for(model).  All constructed at startup.
    anthropic: Any = None       # AsyncAnthropic — direct Anthropic API (claude-* models)
    litellm: Any = None         # AsyncAnthropic(base_url=litellm.example.local) — LiteLLM proxy (legacy)
    openrouter: Any = None     # AsyncAnthropic(base_url=openrouter.ai/api)
    ollama: Any = None          # str: ollama base URL (non-Anthropic wire protocol)

    # ── Module namespaces (stateless) ──────────────────────────────────────
    db: ModuleType | None = None                # database module (public API functions)
    litellm_admin: ModuleType | None = None   # litellm_service module (admin / model-mgmt API)
    task_store: Any = None                    # TaskStore interface
    unified_tasks: Any = None                 # UnifiedTaskService facade
    automation_store: Any = None              # AutomationStore interface
    weather: ModuleType | None = None         # weather_service module
    summary_builder: ModuleType | None = None  # summary_builder module

    # ── Shared primitives ──────────────────────────────────────────────────
    tz: Any = None  # zoneinfo.ZoneInfo

    def llm_for(self, model: str) -> Any:
        """Return the appropriate LLM client for the given model name.

        Returns:
            AsyncAnthropic for Anthropic direct or LiteLLM proxy models.
            str (Ollama base URL) for models in config.ollama_models —
                caller uses _call_ollama(container.ollama, ...) for these.
        """
        from config import config as _cfg
        from model_registry import model_source

        source = model_source(model, _cfg)
        if source == "anthropic":
            return self.anthropic
        if source == "ollama":
            return self.ollama  # str — caller branches to _call_ollama
        if source == "openrouter":
            return self.openrouter or self.litellm
        return self.litellm

    async def aclose(self) -> None:
        """Teardown in reverse construction order.

        Fully wired after the aiohttp session migration (step 13).
        Until then, callers in main.py handle teardown ad-hoc as before.
        """
        if self.presence is not None:
            try:
                await self.presence.stop()
            except Exception:
                log.exception("ServiceContainer.aclose: presence.stop failed")
        if self.ha is not None:
            try:
                await self.ha.close()
            except Exception:
                log.exception("ServiceContainer.aclose: ha.close failed")
        # Close LLM inference clients (they own httpx.AsyncClient handles)
        for _attr in ("anthropic", "litellm", "openrouter"):
            _client = getattr(self, _attr, None)
            if _client is not None:
                try:
                    # AsyncAnthropic uses .close() (async), while raw httpx uses .aclose()
                    if hasattr(_client, "aclose"):
                        await _client.aclose()
                    elif hasattr(_client, "close"):
                        close_fn = _client.close
                        if asyncio.iscoroutinefunction(close_fn):
                            await close_fn()
                        else:
                            close_fn()

                    _owned = getattr(_client, "_owned_http_client", None)
                    if _owned is not None:
                        if hasattr(_owned, "aclose"):
                            await _owned.aclose()
                        elif hasattr(_owned, "close"):
                            close_fn = _owned.close
                            if asyncio.iscoroutinefunction(close_fn):
                                await close_fn()
                            else:
                                close_fn()
                except Exception:
                    log.exception("ServiceContainer.aclose: %s.close failed", _attr)
        if self.session is not None and not self.session.closed:
            try:
                await self.session.close()
            except Exception:
                log.exception("ServiceContainer.aclose: session.close failed")
