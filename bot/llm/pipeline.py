"""Core run loop / pipeline (Phase 4.4 Session 2)."""

from __future__ import annotations

import hashlib
from typing import Any

from executor import ExecutorConfig
from llm.routing import get_executor
from llm.runtime import get_container
from llm.services import build_service_refs


async def run_loop(
    client,
    model: str,
    system: str,
    messages: list[dict],
    config: dict,
    cal_service,
    db_module,
    tz,
    session,
    tools: list[dict],
    *,
    triggered_by: str = "discord",
    group: str | None = None,
    actor_id: str | None = None,
    base_url: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    surface: str = "chat",
    person_id: str | None = None,
    channel_id: str | None = None,
    services: Any = None,
    user_message: str | None = None,
    is_dm: bool = False,
    mode: str | None = None,
    executor_override: str | None = None,
    health_sleep_watch: bool = False,
    health_sleep_prefetch_ok: bool = False,
    tools_advertised: int | None = None,
    tool_domain_count: int | None = None,
) -> str:
    """Route the requested turn through the configured executor."""
    if services is None:
        services = build_service_refs(get_container())
        if db_module is not None:
            services.db = db_module

    from llm.messages import extract_last_user_message

    last_user_message = user_message or extract_last_user_message(messages)

    executor = get_executor(
        surface, services, model=model, user_message=last_user_message,
        executor_override=executor_override,
    )

    prompt_hash = None
    if last_user_message:
        from llm.hashing import hashable_system_prefix

        prompt_hash = hashlib.sha256(
            (last_user_message + hashable_system_prefix(system)).encode("utf-8")
        ).hexdigest()[:16]

    turn_id = None
    try:
        from llm.turn_timer import TurnTimer

        timer = TurnTimer.current()
        turn_id = getattr(timer, "turn_id", None) if timer else None
    except Exception:
        turn_id = None

    exec_config = ExecutorConfig(
        surface=surface,
        model=model,
        shadow=False,
        prompt_hash=prompt_hash,
        person_id=person_id,
        group=group or "family",
        actor_id=actor_id,
        channel_id=channel_id,
        triggered_by=triggered_by,
        session_id=session_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        mode=mode,
        health_sleep_watch=health_sleep_watch,
        health_sleep_prefetch_ok=health_sleep_prefetch_ok,
        tools_advertised=tools_advertised,
        tool_domain_count=tool_domain_count,
    )
    return await executor.run(messages, system, tools, exec_config)
