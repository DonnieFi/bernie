"""Compat layer for legacy callers (Phase 4.4 5e).

Owns _execute_tool and _find_task so claude_service facade can be thin.
"""

from __future__ import annotations

from typing import Any


async def execute_tool(
    tool_name: str,
    tool_input: dict,
    config: dict,
    cal_service,
    db_module,
    tz,
    session,
    notification_router=None,
    group: str | None = None,
    person_id: str = "unknown",
    channel_id: str | None = None,
    hitl_approved: bool = False,
):
    """Legacy compatibility wrapper for executing a single tool via ToolGateway."""
    from tool_gateway import get_tool_gateway
    from executor import ToolContext
    from .runtime import get_container

    gw = get_tool_gateway()

    container = get_container()
    if container:
        services = _build_service_refs(container)
        if db_module is not None:
            services.db = db_module
        if notification_router is not None:
            services.orchestrator = notification_router
    else:
        from executor import ServiceRefs
        services = ServiceRefs(
            calendar=cal_service,
            db=db_module,
            session=session,
            tz=tz,
            orchestrator=notification_router,
        )

    ctx = ToolContext(
        config=config or {},
        person_id=person_id,
        group=group or "family",
        channel_id=channel_id,
        shadow=False,
        executor="legacy_compat",
        services=services,
        prompt_hash="compat",
        hitl_approved=hitl_approved,
    )
    return await gw.execute(tool_name, tool_input, ctx)


async def find_task(db_module, task_id: int | None, title_search: str | None, actor_id: str | None) -> dict | str:
    """Compat wrapper for the task-domain lookup helper."""
    from tools.tasks import _find_task as _task_find_task
    return await _task_find_task(db_module, task_id, title_search, actor_id)


# local helper to avoid cycle at import time for services
def _build_service_refs(container):
    try:
        from .services import build_service_refs
        return build_service_refs(container)
    except Exception:
        from executor import ServiceRefs
        return ServiceRefs()
