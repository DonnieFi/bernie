"""Service refs builder (Phase 4.4 Session 4).

Moved from claude_service._build_service_refs.
claude_service re-exports it for compat.
All fields preserved exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from service_container import ServiceContainer

from executor import ServiceRefs


def build_service_refs(container: ServiceContainer | None = None) -> ServiceRefs:
    """Snapshot every service handler the tool layer might want.

    Best-effort imports — anything that fails resolves to None and handlers
    fall back to their own lazy `from x import y` (tests that monkey-patch
    `sys.modules` keep working). The point of populating fields here is so
    handlers can prefer `ctx.services.x` over a module-level singleton.
    """
    return ServiceRefs(
        calendar=container.calendar if container else None,
        ha=container.ha if container else None,
        db=container.db if container else None,
        session=container.session if container else None,
        orchestrator=container.notification_orchestrator if container else None,
        identity=container.identity if container else None,
        tz=container.tz if container else None,
        network=container.network if container else None,
        weather=container.weather if container else None,
        litellm_admin=container.litellm_admin if container else None,
        llm_for=container.llm_for if container else None,
        task_store=container.task_store if container else None,
        unified_tasks=container.unified_tasks if container else None,
        automation_store=container.automation_store if container else None,
    )
