"""Task + automation tool handlers."""
from __future__ import annotations

from datetime import datetime, timedelta

from tools import ROLE_ADMIN, ROLE_ALL, ROLE_PARENTS, tool


async def _find_task(task_store, task_id, title_search, actor_id):
    """Helper to find a task by ID or fuzzy title match."""

    if task_id:
        task = await task_store.get_task(int(task_id))
        return task if task else f"Task #{task_id} not found."
    if not title_search:
        return "Please provide either a task_id or a task title to identify which one you mean."

    all_tasks = await task_store.list_all_tasks(status="all")
    search_lower = title_search.lower()
    matches = [t for t in all_tasks if search_lower in t["title"].lower()]
    if not matches:
        return f"Couldn't find any task matching '{title_search}'."
    if len(matches) > 1:
        exact = [m for m in matches if m["title"].lower() == search_lower]
        if len(exact) == 1:
            return exact[0]
        lines = [f"Found {len(matches)} matches for '{title_search}'. Which one did you mean?"]
        for m in matches[:5]:
            lines.append(f"• #{m['id']}: {m['title']} ({m['assigned_to']})")
        return "\n".join(lines)
    return matches[0]


@tool(
    name="create_task",
    description=(
        "Create a new task or chore for a family member. Bernie will notify "
        "the assignee and track status."
    ),
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "title":       {"type": "string", "description": "Task title"},
            "details":     {"type": "string", "description": "Optional instructions or notes"},
            "assigned_to": {"type": "string", "description": "Person to do the task"},
            "due_at":      {"type": "string", "description": "Optional ISO 8601 datetime"},
            "priority":    {"type": "string", "enum": ["low", "normal", "high"], "default": "normal"},
            "category":    {"type": "string", "description": "Optional category"},
        },
        "required": ["title", "assigned_to"],
    },
    role_required=ROLE_PARENTS,
    tier=2,
)
async def handle_create_task(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called create_task({args})]"

    unified_tasks = getattr(ctx.services, "unified_tasks", None)
    if not unified_tasks:
        return "unified_tasks service is not available in the current context."

    actor_id = ctx.person_id

    from services.unified_task_service import TaskValidationError

    try:
        task = await unified_tasks.create_chore_task(
            title=args["title"],
            details=args.get("details", ""),
            assigned_to=args["assigned_to"],
            assigned_by=actor_id or "bernie",
            due_at=args.get("due_at"),
            priority=args.get("priority", "normal"),
            category=args.get("category", "Task"),
        )
    except TaskValidationError as e:
        return str(e)

    from constants import registry as person_registry
    from task_access import registry_person_id

    assignee = registry_person_id(task.get("assigned_to")) or task.get("assigned_to", "")
    name = person_registry.display_name(assignee) if assignee else "assignee"
    return f"Task #{task['id']} created for {name}."


@tool(
    name="list_tasks",
    description="List active tasks for a person or the whole family.",
    input_schema={
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "Person's name or 'all'"},
            "status": {"type": "string", "enum": ["pending", "done", "approved", "all"], "default": "pending"},
        },
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_list_tasks(args: dict, ctx) -> str:
    task_store = ctx.services.task_store
    from constants import registry as person_registry

    person = args.get("person", "all").lower()
    status = args.get("status", "pending")
    if person == "all":
        tasks = await task_store.list_all_tasks(status=status)
    else:
        pid = person_registry.resolve(person) or person
        tasks = await task_store.list_tasks_for_person(pid, status=status)
    if not tasks:
        return f"No {status} tasks found for {person}."
    lines = []
    for t in tasks[:15]:
        due = f" (due {t['due_at']})" if t.get("due_at") else ""
        lines.append(
            f"• #{t['id']} [{t['status']}] {t['title']} - Assigned to: {t['assigned_to']}{due}"
        )
    return "\n".join(lines)


@tool(
    name="complete_task",
    description=(
        "Mark a task as completed. If assigned by a parent, it will wait for "
        "approval."
    ),
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "ID of the task (if known)"},
            "title":   {"type": "string",  "description": "Search by title"},
            "note":    {"type": "string",  "description": "Optional completion note"},
        },
    },
    role_required=ROLE_ALL,
    tier=2,
)
async def handle_complete_task(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called complete_task({args})]"
    unified_tasks = getattr(ctx.services, "unified_tasks", None)
    if not unified_tasks:
        return "unified_tasks service is not available in the current context."

    actor_id = ctx.person_id
    task_or_msg = await _find_task(ctx.services.task_store, args.get("task_id"), args.get("title"), actor_id)
    if isinstance(task_or_msg, str):
        return task_or_msg
    task = task_or_msg
    task_id = task["id"]
    note = args.get("note", "").strip()

    from services.unified_task_service import TaskValidationError
    try:
        updated = await unified_tasks.complete_task(
            task_id,
            actor_id=actor_id or "bernie",
            note=note,
            via="conversational",
        )
    except TaskValidationError as e:
        return str(e)

    if task.get("requires_approval") or updated.get("requires_approval"):
        return f"Task #{task_id} marked as done. It is now awaiting parental approval."
    return f"Task #{task_id} completed and closed."


@tool(
    name="approve_task",
    description="Approve a completed task or send it back to pending (reopen).",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "task_id":  {"type": "integer"},
            "title":    {"type": "string"},
            "approved": {"type": "boolean", "description": "True to approve, False to reopen"},
        },
        "required": ["approved"],
    },
    role_required=ROLE_PARENTS,
    tier=2,
)
async def handle_approve_task(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called approve_task({args})]"
    unified_tasks = getattr(ctx.services, "unified_tasks", None)
    if not unified_tasks:
        return "unified_tasks service is not available in the current context."

    actor_id = ctx.person_id
    task_or_msg = await _find_task(ctx.services.task_store, args.get("task_id"), args.get("title"), actor_id)
    if isinstance(task_or_msg, str):
        return task_or_msg
    task_id = task_or_msg["id"]
    approved = args["approved"]

    from services.unified_task_service import TaskValidationError
    try:
        await unified_tasks.approve_task(
            task_id,
            actor_id=actor_id or "agent:bernie",
            approved=approved,
        )
    except TaskValidationError as e:
        return str(e)

    return f"Task #{task_id} {'approved' if approved else 'reopened and sent back'}."


@tool(
    name="update_task",
    description="Update details, priority, due date, assignee, or category of an existing task.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "task_id":      {"type": "integer"},
            "title_search": {"type": "string"},
            "title":        {"type": "string"},
            "details":      {"type": "string"},
            "assigned_to":  {"type": "string"},
            "priority":     {"type": "string", "enum": ["low", "normal", "high"]},
            "category":     {"type": "string"},
            "due_at":       {"type": "string"},
        },
    },
    role_required=ROLE_PARENTS,
    tier=2,
)
async def handle_update_task(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called update_task({args})]"
    unified_tasks = getattr(ctx.services, "unified_tasks", None)
    if not unified_tasks:
        return "unified_tasks service is not available in the current context."

    actor_id = ctx.person_id
    task_or_msg = await _find_task(ctx.services.task_store, args.get("task_id"), args.get("title_search"), actor_id)
    if isinstance(task_or_msg, str):
        return task_or_msg
    task_id = task_or_msg["id"]
    updates = {
        k: v for k, v in args.items()
        if k in ("title", "details", "priority", "due_at", "category", "in_progress", "assigned_to")
    }
    if not updates:
        return f"Task #{task_id} — no fields to update."

    from services.unified_task_service import TaskValidationError
    try:
        updated = await unified_tasks.update_task(
            task_id,
            actor_id=actor_id or "bernie",
            updates=updates,
        )
    except TaskValidationError as e:
        return str(e)

    res = f"Task #{task_id} updated successfully."
    if "assigned_to" in updates:
        res += f" Re-assigned to {updated.get('assigned_to', updates['assigned_to'])}."
    return res


@tool(
    name="delete_task",
    description="Permanently delete a task. Admin/Parents only.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "integer"},
            "title":   {"type": "string", "description": "Search by title if ID unknown"},
        },
    },
    role_required=ROLE_PARENTS,
    tier=3,
)
async def handle_delete_task(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called delete_task({args})]"
    unified_tasks = getattr(ctx.services, "unified_tasks", None)
    if not unified_tasks:
        return "unified_tasks service is not available in the current context."

    actor_id = ctx.person_id
    task_or_msg = await _find_task(ctx.services.task_store, args.get("task_id"), args.get("title"), actor_id)
    if isinstance(task_or_msg, str):
        return task_or_msg
    task = task_or_msg
    task_id = task["id"]

    from services.unified_task_service import TaskValidationError
    try:
        await unified_tasks.delete_task(task_id, actor_id=actor_id or "bernie")
    except TaskValidationError as e:
        return str(e)

    return f"Task #{task_id} ('{task.get('title')}') deleted permanently."


@tool(
    name="snooze_task",
    description="Delay a task notification. Choose a preset or specify a duration.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "integer"},
            "title":   {"type": "string"},
            "preset":  {"type": "string", "enum": ["30m", "1h", "tomorrow"]},
        },
        "required": ["preset"],
    },
    role_required=ROLE_ALL,
    tier=2,
)
async def handle_snooze_task(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called snooze_task({args})]"
    unified_tasks = getattr(ctx.services, "unified_tasks", None)
    if not unified_tasks:
        return "unified_tasks service is not available in the current context."

    actor_id = ctx.person_id
    task_or_msg = await _find_task(ctx.services.task_store, args.get("task_id"), args.get("title"), actor_id)
    if isinstance(task_or_msg, str):
        return task_or_msg
    task_id = task_or_msg["id"]
    preset = args["preset"]
    now = datetime.now()
    if preset == "30m":
        until = (now + timedelta(minutes=30)).isoformat()
    elif preset == "1h":
        until = (now + timedelta(hours=1)).isoformat()
    else:
        until = (now + timedelta(days=1)).replace(
            hour=8, minute=0, second=0, microsecond=0
        ).isoformat()

    from services.unified_task_service import TaskValidationError
    try:
        await unified_tasks.snooze_task(
            task_id,
            actor_id=actor_id or "agent:bernie",
            snooze_until=until,
        )
    except TaskValidationError as e:
        return str(e)

    return f"Task #{task_id} snoozed until {preset}."


@tool(
    name="decline_task",
    description="State that a task cannot or will not be completed and provide a reason.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "integer"},
            "title":   {"type": "string"},
            "reason":  {"type": "string", "description": "Why the task is being declined"},
        },
        "required": ["reason"],
    },
    role_required=ROLE_ALL,
    tier=2,
)
async def handle_decline_task(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called decline_task({args})]"
    unified_tasks = getattr(ctx.services, "unified_tasks", None)
    if not unified_tasks:
        return "unified_tasks service is not available in the current context."

    actor_id = ctx.person_id
    task_or_msg = await _find_task(ctx.services.task_store, args.get("task_id"), args.get("title"), actor_id)
    if isinstance(task_or_msg, str):
        return task_or_msg
    task_id = task_or_msg["id"]
    reason = args["reason"]

    from services.unified_task_service import TaskValidationError
    try:
        await unified_tasks.decline_task(
            task_id,
            actor_id=actor_id or "bernie",
            reason=reason,
        )
    except TaskValidationError as e:
        return str(e)

    return f"Task #{task_id} declined and removed."


# ── Automations ─────────────────────────────────────────────────────────────
@tool(
    name="create_automation",
    description=(
        "Create a recurring reminder or automated notification. "
        "Prefer natural schedule_nl like 'every Sunday at 9am' or 'daily 07:30'; "
        "or pass schedule_kind + schedule explicitly."
    ),
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "title":         {"type": "string"},
            "message":       {"type": "string"},
            "schedule_kind": {
                "type": "string",
                "enum": ["cron", "daily", "weekly", "hourly", "once"],
                "description": "Optional if schedule_nl is set",
            },
            "schedule": {
                "type": "string",
                "description": "HH:MM, 'dow HH:MM', cron expr, etc. Optional if schedule_nl is set",
            },
            "schedule_nl": {
                "type": "string",
                "description": (
                    "Natural language schedule, e.g. 'every Sunday at 9am', "
                    "'daily at 7:30', 'every hour', 'hourly at :15'"
                ),
            },
            "audience":      {"type": "string", "enum": ["self", "everyone"], "default": "self"},
        },
        "required": ["title", "message"],
    },
    role_required=ROLE_PARENTS,
    tier=2,
)
async def handle_create_automation(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called create_automation({args})]"
    store = ctx.services.automation_store or ctx.services.db

    actor_id = ctx.person_id
    title = args["title"]
    message = args["message"]
    audience = args.get("audience", "self")
    payload: dict = {}
    kind = args.get("schedule_kind")
    schedule = args.get("schedule") or ""
    schedule_nl = (args.get("schedule_nl") or "").strip()

    try:
        from utils.discord_helpers import next_automation_run, parse_nl_schedule, weekday_num

        # family-bot-5hy.12: NL first, then explicit kind+schedule
        if schedule_nl:
            parsed = parse_nl_schedule(schedule_nl)
            if not parsed:
                return (
                    f"Could not parse schedule_nl {schedule_nl!r}. "
                    "Try e.g. 'every Sunday at 9am', 'daily 07:30', 'every hour'."
                )
            kind, payload = parsed
        else:
            if not kind or not schedule:
                return "Provide schedule_nl or both schedule_kind and schedule."
            if kind == "cron":
                payload = {"expr": schedule}
            elif kind == "daily":
                payload = {"time": schedule}
            elif kind == "hourly":
                payload = {"minute": int(schedule)}
            elif kind == "once":
                payload = {"run_at": schedule}
            elif kind == "weekly":
                parts = schedule.split()
                if len(parts) < 2:
                    # try NL on the schedule string alone
                    parsed = parse_nl_schedule(schedule)
                    if parsed and parsed[0] == "weekly":
                        kind, payload = parsed
                    else:
                        return "weekly schedule needs 'Day HH:MM' or schedule_nl like 'every Sunday at 9am'"
                else:
                    payload = {"day_of_week": weekday_num(parts[0]), "time": parts[1]}
            else:
                return f"Unknown schedule_kind: {kind}"

        tz_name = ctx.config.get("timezone", "America/Halifax")
        next_run = next_automation_run(kind, payload, tz_name)
        if not next_run:
            return "Error: This schedule does not produce a future run time."

        auto = await store.create_automation(
            title=title,
            message=message,
            person_id="everyone" if audience == "everyone" else (actor_id or "bernie"),
            schedule_kind=kind,
            schedule_payload=payload,
            timezone=tz_name,
            created_by=actor_id or "bernie",
            audience_scope=audience,
            next_run_at=next_run.isoformat(),
        )
        return (
            f"Automation #{auto['id']} created ({kind} {payload}). "
            f"Next run: {next_run.strftime('%Y-%m-%d %H:%M')}"
        )
    except Exception as e:
        return f"Failed to create automation: {e}"


@tool(
    name="list_automations",
    description="List active recurring automations (filterable by active_only).",
    input_schema={
        "type": "object",
        "properties": {
            "active_only": {
                "type": "boolean",
                "description": "If true, only show enabled automations (default true)",
            },
        },
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_list_automations(args: dict, ctx) -> str:
    store = ctx.services.automation_store or ctx.services.db
    autos = await store.list_all_automations()
    active_only = args.get("active_only", True)
    if active_only:
        autos = [a for a in autos if a.get("is_active")]
    if not autos:
        return "No automations found."
    lines = []
    for a in autos:
        status = "on" if a["is_active"] else "off"
        payload = a.get("schedule_payload") or {}
        lines.append(
            f"• #{a['id']} [{status}] {a['title']} ({a['schedule_kind']} {payload}) "
            f"— Next: {a.get('next_run_at')}"
        )
    return "\n".join(lines)


@tool(
    name="toggle_automation",
    description="Enable or disable an automation by ID.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "id":      {"type": "integer"},
            "enabled": {"type": "boolean"},
        },
        "required": ["id", "enabled"],
    },
    role_required=ROLE_PARENTS,
    tier=3,
)
async def handle_toggle_automation(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called toggle_automation({args})]"
    store = ctx.services.automation_store or ctx.services.db
    auto_id = args["id"]
    enabled = args["enabled"]
    try:
        updated = await store.set_automation_active(auto_id, enabled)
        if not updated:
            return "Automation not found."
        return f"Automation #{auto_id} {'enabled' if enabled else 'disabled'}."
    except Exception as e:
        return f"Failed to toggle automation: {e}"


@tool(
    name="delete_automation",
    description="Permanently delete a recurring automation. Admin/Parents only.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
        },
        "required": ["id"],
    },
    role_required=ROLE_PARENTS,
    tier=3,
)
async def handle_delete_automation(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called delete_automation({args})]"
    store = ctx.services.automation_store or ctx.services.db
    auto_id = args["id"]
    try:
        await store.delete_automation(auto_id)
        return f"Automation #{auto_id} deleted permanently."
    except Exception as e:
        return f"Failed to delete automation: {e}"
