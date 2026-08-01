"""Kanban agent tools — create/work first-class agent tasks on the unified board."""
from __future__ import annotations
from tools import ROLE_ADMIN, ROLE_BERNIE, ROLE_PARENTS, tool


def _require_task_id(ctx) -> int | None:
    """Task-scoped tools may only act on the task bound to their context."""
    return getattr(ctx, "task_id", None)


@tool(
    name="kanban_show",
    description="Show the agent's currently-assigned task: details, status, run history, links.",
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_BERNIE, domain="kanban",
    tier=1,
)
async def handle_kanban_show(args: dict, ctx) -> str:
    store = ctx.services.task_store
    tid = _require_task_id(ctx)
    if not tid:
        return "kanban_show: no task bound to this context."
    t = await store.get_task(tid)
    if not t:
        return f"Task #{tid} not found."
    runs = await store.list_executions(tid)
    return (f"#{t['id']} [{t['kanban_status']}] {t['title']} (type={t['type']}, "
            f"assignee={t['assigned_to']})\n{t['details']}\nruns: {len(runs)}")


@tool(
    name="kanban_create",
    description=("Create an agent task on the board: research (a lookup), bernie (Bernie's own work), "
                 "or code (nanobot). Not for chores — use create_task for those."),
    is_write=True,
    input_schema={"type": "object", "properties": {
        "type": {"type": "string", "enum": ["research", "bernie", "code"]},
        "title": {"type": "string"}, "details": {"type": "string"},
        "assigned_to": {"type": "string", "description": "Namespaced id, e.g. agent:bernie"},
        "priority": {"type": "string", "enum": ["low", "normal", "high"], "default": "normal"},
        "horizon": {"type": "string", "description": "YYYY-MM or 'someday'"},
    }, "required": ["type", "title"]},
    role_required=ROLE_PARENTS, domain="kanban",
    tier=2,
)
async def handle_kanban_create(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called kanban_create({args})]"
    unified_tasks = getattr(ctx.services, "unified_tasks", None)
    if not unified_tasks:
        return "unified_tasks service is not available in the current context."

    ttype = args["type"]
    assignee = args.get("assigned_to")
    from services.unified_task_service import TaskValidationError

    try:
        t = await unified_tasks.create_agent_task(
            task_type=ttype,
            title=args["title"],
            details=args.get("details", ""),
            assigned_to=assignee,
            assigned_by=ctx.person_id or "agent:bernie",
            priority=args.get("priority", "normal"),
            horizon=args.get("horizon"),
        )
    except TaskValidationError as e:
        return str(e)

    ret_assignee = t.get("assigned_to")
    return f"{ttype} task #{t['id']} created" + (f" for {ret_assignee}." if ret_assignee else " (open to claim).")


@tool(name="kanban_heartbeat", description="Report progress on the bound task (keeps it alive).",
      is_write=True, input_schema={"type": "object", "properties": {"note": {"type": "string"}}},
      role_required=ROLE_BERNIE, domain="kanban",
      tier=2,)
async def handle_kanban_heartbeat(args: dict, ctx) -> str:
    if ctx.shadow: return "[shadow: kanban_heartbeat]"
    store = ctx.services.task_store
    tid = _require_task_id(ctx)
    if not tid: return "kanban_heartbeat: no task bound to this context."
    await store.update_unified_task_heartbeat(tid)
    if args.get("note"): await store.add_task_event(tid, "heartbeat", ctx.person_id, {"note": args["note"]})
    return f"heartbeat #{tid}."


@tool(name="kanban_comment", description="Append a note to the bound task's thread.",
      is_write=True, input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
      role_required=ROLE_BERNIE, domain="kanban",
      tier=2,)
async def handle_kanban_comment(args: dict, ctx) -> str:
    if ctx.shadow: return "[shadow: kanban_comment]"
    store = ctx.services.task_store
    tid = _require_task_id(ctx)
    if not tid: return "kanban_comment: no task bound to this context."
    await store.add_task_event(tid, "comment", ctx.person_id, {"text": args["text"]})
    return f"commented on #{tid}."


@tool(name="kanban_complete", description="Mark the bound task done with a summary; records the run.",
      is_write=True, input_schema={"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
      role_required=ROLE_BERNIE, domain="kanban",
      tier=2,)
async def handle_kanban_complete(args: dict, ctx) -> str:
    if ctx.shadow: return "[shadow: kanban_complete]"
    unified_tasks = getattr(ctx.services, "unified_tasks", None)
    if not unified_tasks:
        return "unified_tasks service is not available in the current context."
    tid = _require_task_id(ctx)
    if not tid: return "kanban_complete: no task bound to this context."

    from services.unified_task_service import TaskValidationError
    try:
        await unified_tasks.complete_task(tid, actor_id=ctx.person_id or "agent:bernie", note=args["summary"], via="kanban")
    except TaskValidationError as e:
        return str(e)
    return f"#{tid} completed."


@tool(name="kanban_block", description="Flag the bound task blocked (needs human help); raises a ping.",
      is_write=True, input_schema={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
      role_required=ROLE_BERNIE, domain="kanban",
      tier=2,)
async def handle_kanban_block(args: dict, ctx) -> str:
    if ctx.shadow: return "[shadow: kanban_block]"
    unified_tasks = getattr(ctx.services, "unified_tasks", None)
    if not unified_tasks:
        return "unified_tasks service is not available in the current context."
    tid = _require_task_id(ctx)
    if not tid: return "kanban_block: no task bound to this context."
    reason = args["reason"]

    from services.unified_task_service import TaskValidationError
    try:
        await unified_tasks.move_task(tid, "blocked", actor_id=ctx.person_id or "agent:bernie", reason=reason, via="kanban")
    except TaskValidationError as e:
        return str(e)
    return f"#{tid} blocked: {reason}"


@tool(name="kanban_link", description="Add a parent→child dependency between two tasks (cycles rejected).",
      is_write=True, input_schema={"type": "object", "properties": {
          "parent_id": {"type": "integer"}, "child_id": {"type": "integer"}},
          "required": ["parent_id", "child_id"]},
      role_required=ROLE_BERNIE, domain="kanban",
      tier=2,)
async def handle_kanban_link(args: dict, ctx) -> str:
    if ctx.shadow: return "[shadow: kanban_link]"
    store = ctx.services.task_store
    ok = await store.link_tasks(int(args["parent_id"]), int(args["child_id"]))
    if not ok:
        return "Link rejected — it would create a cycle (or is a self-link)."
    await store.promote_ready_tasks()
    return f"linked #{args['parent_id']} → #{args['child_id']}."


@tool(name="reassign_task", description="Reassign a task to a different person/agent (gated by task type).",
      is_write=True, input_schema={"type": "object", "properties": {
          "task_id": {"type": "integer"}, "assigned_to": {"type": "string"}}, "required": ["task_id", "assigned_to"]},
      role_required=ROLE_PARENTS, domain="kanban",
      tier=2,)
async def handle_reassign_task(args: dict, ctx) -> str:
    if ctx.shadow:
        return "[shadow: reassign_task]"
    unified_tasks = getattr(ctx.services, "unified_tasks", None)
    if not unified_tasks:
        return "unified_tasks service is not available in the current context."
    from services.unified_task_service import TaskValidationError
    try:
        updated = await unified_tasks.reassign_task(
            int(args["task_id"]),
            actor_id=ctx.person_id or "agent:bernie",
            assigned_to=args["assigned_to"],
        )
    except TaskValidationError as e:
        return str(e)
    assignee = updated.get("assigned_to") or args["assigned_to"]
    return f"#{updated['id']} reassigned to {assignee}."
