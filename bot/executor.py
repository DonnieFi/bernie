from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass
class ServiceRefs:
    """Service handles passed into every tool handler via ToolContext.

    Production: `_run_loop` populates these with the actual module-level
    singletons (ha_service, network_service, …) or modules-as-namespaces
    (weather_service, litellm_service, database).

    Tests: pass mocks directly — handlers should prefer `ctx.services.X`
    over `from X_service import X` so monkeypatching `sys.modules` isn't
    required. Handlers may still fall back to a lazy import when the field
    is None (e.g. during the migration window before Task 8 wires
    _run_loop to populate ServiceRefs).
    """
    calendar: Any = None     # CalendarService | None
    ha: Any = None           # HAService singleton — get_state / get_live_states / etc.
    db: Any = None           # database module (functions)
    session: Any = None      # aiohttp.ClientSession
    orchestrator: Any = None # NotificationRouter
    identity: Any = None     # IdentityService singleton
    tz: Any = None           # zoneinfo.ZoneInfo
    network: Any = None      # network_service singleton — get_devices()
    weather: Any = None      # weather_service module — get_weather_for_request, format_weather_report
    litellm_admin: Any = None  # litellm_service module — list_models, add_openrouter_model, delete_model
    llm_for: Any = None        # callable(model_name) → AsyncAnthropic | str (Ollama URL)
    task_store: Any = None     # TaskStore interface
    unified_tasks: Any = None  # UnifiedTaskService facade
    automation_store: Any = None  # AutomationStore interface


@dataclass
class ExecutorConfig:
    surface: Literal["chat", "workers"]  # "workers" kept only for legacy metadata in ToolContext; see tombstone note in llm/routing.py and cognitive_tasks polling in worker.py
    model: str
    shadow: bool = False          # True for harness_shadow runs
    prompt_hash: str | None = None
    person_id: str | None = None  # canonical identity_nodes.id
    group: str = "family"         # permission role: family/parents/kids/admin
    actor_id: str | None = None
    channel_id: str | None = None
    triggered_by: str = "discord"
    session_id: str | None = None
    conversation_id: str | None = None
    turn_id: str | None = None
    mode: str | None = None  # Phase 28 Wave 2c: active mode slug for tagging
    health_sleep_watch: bool = False       # True when turn matched health/sleep intent
    health_sleep_prefetch_ok: bool = False  # True when prefetch returned both sources
    task_id: int | None = None
    tools_advertised: int | None = None    # Phase 39: schemas sent to model this turn
    tool_domain_count: int | None = None   # Phase 39: len(resolved domain list), None = full surface


@dataclass
class ToolContext:
    config: dict
    person_id: str | None   # canonical identity_nodes.id
    group: str              # permission role: family/parents/kids/admin
    channel_id: str | None
    shadow: bool            # True for harness_shadow runs — write tools blocked
    executor: str           # 'native' | 'smol' — for Langfuse span metadata
    services: ServiceRefs
    prompt_hash: str | None = None
    task_id: int | None = None  # bound by the worker supervisor for task-scoped kanban_* tools
    mode: str | None = None
    hitl_approved: bool = False
    hitl_pending_id: int | None = None


@runtime_checkable
class Executor(Protocol):
    async def run(
        self,
        messages: list[dict],
        system: str | list[dict],
        tools: list[dict],
        config: ExecutorConfig,
    ) -> str:
        ...
