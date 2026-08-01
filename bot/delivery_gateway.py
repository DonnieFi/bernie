"""System-path tool execution through ToolGateway (Phase 29 no-bypass)."""

from __future__ import annotations

from executor import ServiceRefs, ToolContext


def _resolve_services(container) -> ServiceRefs:
    if container is None:
        return ServiceRefs()
    return ServiceRefs(
        db=getattr(container, "db", None),
        orchestrator=getattr(container, "notification_orchestrator", None),
        task_store=getattr(container, "task_store", None),
        unified_tasks=getattr(container, "unified_tasks", None),
    )


def _get_gateway():
    from tool_gateway import get_tool_gateway

    return get_tool_gateway()


def _tool_result_ok(text: str) -> bool:
    """Return False when ToolGateway returned a user-facing failure string."""
    lower = text.lower()
    if text.startswith("Unknown tool:"):
        return False
    if lower.startswith("email send failed:"):
        return False
    if lower.startswith("email blocked by policy"):
        return False
    if "could not post email draft" in lower:
        return False
    if "nothing was sent" in lower:
        return False
    return True


def system_tool_context(
    *,
    config: dict,
    container=None,
    person_id: str = "agent:research-worker",
    hitl_approved: bool = True,
    executor: str = "workers",  # legacy default string for ToolContext.executor metadata; real workers no longer use executor surface (see cognitive_tasks queue + worker.py)
) -> ToolContext:
    return ToolContext(
        config=config,
        person_id=person_id,
        group="system",
        channel_id=None,
        shadow=False,
        executor=executor,
        services=_resolve_services(container),
        hitl_approved=hitl_approved,
    )


async def execute_system_tool(
    tool_name: str,
    args: dict,
    *,
    config: dict,
    container=None,
    person_id: str = "agent:research-worker",
    hitl_approved: bool = True,
) -> str:
    gateway = _get_gateway()
    ctx = system_tool_context(
        config=config,
        container=container,
        person_id=person_id,
        hitl_approved=hitl_approved,
    )
    result = await gateway.execute(tool_name, args, ctx)
    text = result if isinstance(result, str) else str(result)
    if "requires admin approval" in text:
        raise RuntimeError(text)
    if not _tool_result_ok(text):
        raise RuntimeError(text)
    return text


async def send_email_via_gateway(
    *,
    to: str,
    subject: str,
    body: str,
    config: dict,
    container=None,
    cc: str | None = None,
    person_id: str = "agent:research-worker",
) -> str:
    args: dict = {"to": to, "subject": subject, "body": body}
    if cc:
        args["cc"] = cc
    return await execute_system_tool(
        "send_email",
        args,
        config=config,
        container=container,
        person_id=person_id,
        hitl_approved=True,
    )
