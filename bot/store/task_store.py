from typing import Protocol, Any


def _paginate_rows(rows: list[dict], *, limit: int | None, offset: int | None) -> list[dict]:
    from database.tasks import clamp_list_limit, clamp_list_offset

    off = clamp_list_offset(offset)
    lim = clamp_list_limit(limit)
    return rows[off : off + lim]


async def _routed_write(op: str, /, **kwargs: Any) -> Any:
    """Route SQLite writes through cognition when ROLE is api (40A-4)."""
    from db_client import cognition_db_write

    return await cognition_db_write(op, **kwargs)


class TaskStore(Protocol):
    async def get_task(self, task_id: int) -> dict | None: ...
    async def create_agent_task(self, type: str, title: str, details: str, assigned_to: str | None, assigned_by: str, priority: str, horizon: str | None, visibility: str) -> dict: ...
    async def list_executions(self, task_id: int) -> list[dict]: ...
    async def update_unified_task_heartbeat(self, task_id: int) -> None: ...
    async def add_task_event(self, task_id: int, event_type: str, actor_id: str | None, payload: dict) -> None: ...
    async def start_execution(self, task_id: int, run_id: str) -> None: ...
    async def finish_execution(self, run_id: str, status: str, logs: str, metrics: dict | None = None) -> None: ...
    async def complete_task(self, task_id: int, completion_note: str = "") -> dict | None: ...
    async def block_task(self, task_id: int, reason: str) -> None: ...
    async def link_tasks(self, parent_id: int, child_id: int) -> bool: ...
    async def promote_ready_tasks(self) -> None: ...
    async def reassign_task(self, task_id: int, assigned_to: str) -> dict | None: ...
    async def get_task_links(self, task_id: int) -> dict: ...
    async def list_task_events(self, task_id: int) -> list[dict]: ...
    def is_system_virtual_task_id(self, task_id: int) -> bool: ...
    async def get_system_task_detail(self, task_id: int) -> dict | None: ...
    async def set_kanban_status(self, task_id: int, status: str) -> dict | None: ...
    async def set_unified_task_running(self, task_id: int) -> None: ...
    async def store_message_mapping(self, message_id: int, event_id: str, event_title: str = None, message_type: str = "event") -> None: ...
    async def log_activity(self, event_type: str, description: str, meta: str = None, channel: str = None, person_id: str = None) -> None: ...

    async def create_task(
        self,
        title: str,
        assigned_to: str,
        assigned_by: str,
        details: str = "",
        due_at: str | None = None,
        requires_approval: bool = False,
        approver_person_id: str | None = None,
        remind_visibility: str = "private",
        remind_channel_id: int | None = None,
        category: str = "Task",
        is_recurring: bool = False,
        in_progress: bool = False,
        priority: str = "normal",
        horizon: str | None = None,
    ) -> dict: ...
    async def convert_task_type(self, task_id: int, new_type: str, *, assignee: str | None = None) -> dict | None: ...
    async def update_task(self, task_id: int, updates: dict) -> dict | None: ...
    async def list_tasks_for_person(
        self,
        person_id: str,
        status: str = "all",
        include_assigned_by: bool = True,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]: ...
    async def list_all_tasks(
        self,
        status: str = "all",
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]: ...
    async def snooze_task(self, task_id: int, snooze_until: str) -> dict | None: ...
    async def approve_task(self, task_id: int, approved: bool) -> dict | None: ...
    async def delete_task(self, task_id: int) -> None: ...



class SQLiteTaskStore(TaskStore):
    """Delegates reads to database.py; writes route via cognition (40A-4)."""

    async def get_task(self, task_id: int) -> dict | None:
        import database as db
        return await db.get_task(task_id)

    async def create_agent_task(self, type: str, title: str, details: str, assigned_to: str | None, assigned_by: str, priority: str, horizon: str | None, visibility: str) -> dict:
        return await _routed_write(
            "create_agent_task",
            type=type,
            title=title,
            details=details,
            assigned_to=assigned_to,
            assigned_by=assigned_by,
            priority=priority,
            horizon=horizon,
            visibility=visibility,
        )

    async def list_executions(self, task_id: int) -> list[dict]:
        import database as db
        return await db.list_executions(task_id)

    async def update_unified_task_heartbeat(self, task_id: int) -> None:
        await _routed_write("update_unified_task_heartbeat", task_id=task_id)

    async def add_task_event(self, task_id: int, event_type: str, actor_id: str | None, payload: dict) -> None:
        await _routed_write(
            "add_task_event",
            task_id=task_id,
            event_type=event_type,
            actor_person_id=actor_id,
            metadata=payload,
        )

    async def start_execution(self, task_id: int, run_id: str) -> None:
        await _routed_write("start_execution", task_id=task_id, execution_id=run_id)

    async def finish_execution(self, run_id: str, status: str, logs: str, metrics: dict | None = None) -> None:
        await _routed_write(
            "finish_execution",
            execution_id=run_id,
            status=status,
            logs=logs,
            metrics=metrics,
        )

    async def complete_task(self, task_id: int, completion_note: str = "") -> dict | None:
        return await _routed_write("complete_task", task_id=task_id, completion_note=completion_note)

    async def block_task(self, task_id: int, reason: str) -> None:
        await _routed_write("block_task", task_id=task_id, reason=reason)

    async def link_tasks(self, parent_id: int, child_id: int) -> bool:
        return await _routed_write("link_tasks", parent_id=parent_id, child_id=child_id)

    async def promote_ready_tasks(self) -> None:
        await _routed_write("promote_ready_tasks")

    async def reassign_task(self, task_id: int, assigned_to: str) -> dict | None:
        return await _routed_write("reassign_task", task_id=task_id, assignee=assigned_to)

    async def get_task_links(self, task_id: int) -> dict:
        import database as db
        return await db.get_task_links(task_id)

    async def list_task_events(self, task_id: int) -> list[dict]:
        import database as db
        return await db.list_task_events(task_id)

    def is_system_virtual_task_id(self, task_id: int) -> bool:
        import database as db
        return db.is_system_virtual_task_id(task_id)

    async def get_system_task_detail(self, task_id: int) -> dict | None:
        import database as db
        return await db.get_system_task_detail(task_id)

    async def set_kanban_status(self, task_id: int, status: str) -> dict | None:
        return await _routed_write("set_kanban_status", task_id=task_id, status=status)

    async def set_unified_task_running(self, task_id: int) -> None:
        await _routed_write("set_unified_task_running", task_id=task_id)

    async def create_task(
        self,
        title: str,
        assigned_to: str,
        assigned_by: str,
        details: str = "",
        due_at: str | None = None,
        requires_approval: bool = False,
        approver_person_id: str | None = None,
        remind_visibility: str = "private",
        remind_channel_id: int | None = None,
        category: str = "Task",
        is_recurring: bool = False,
        in_progress: bool = False,
        priority: str = "normal",
        horizon: str | None = None,
    ) -> dict:
        return await _routed_write(
            "create_task",
            title=title,
            assigned_to=assigned_to,
            assigned_by=assigned_by,
            details=details,
            due_at=due_at,
            requires_approval=requires_approval,
            approver_person_id=approver_person_id,
            remind_visibility=remind_visibility,
            remind_channel_id=remind_channel_id,
            category=category,
            is_recurring=is_recurring,
            in_progress=in_progress,
            priority=priority,
            horizon=horizon,
        )

    async def convert_task_type(self, task_id: int, new_type: str, *, assignee: str | None = None) -> dict | None:
        return await _routed_write(
            "convert_task_type", task_id=task_id, new_type=new_type, assignee=assignee
        )

    async def update_task(self, task_id: int, updates: dict) -> dict | None:
        return await _routed_write("update_task", task_id=task_id, updates=updates)

    async def list_tasks_for_person(
        self,
        person_id: str,
        status: str = "all",
        include_assigned_by: bool = True,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]:
        import database as db
        return await db.list_tasks_for_person(
            person_id, status=status, include_assigned_by=include_assigned_by, limit=limit, offset=offset
        )

    async def list_all_tasks(
        self,
        status: str = "all",
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]:
        import database as db
        return await db.list_all_tasks(status=status, limit=limit, offset=offset)

    async def snooze_task(self, task_id: int, snooze_until: str) -> dict | None:
        return await _routed_write("snooze_task", task_id=task_id, snooze_until=snooze_until)

    async def approve_task(self, task_id: int, approved: bool) -> dict | None:
        return await _routed_write("approve_task", task_id=task_id, approved=approved)

    async def delete_task(self, task_id: int) -> None:
        await _routed_write("delete_task", task_id=task_id)

    async def store_message_mapping(self, message_id: int, event_id: str, event_title: str = None, message_type: str = "event") -> None:
        await _routed_write(
            "store_message_mapping",
            message_id=message_id,
            event_id=event_id,
            event_title=event_title,
            message_type=message_type,
        )

    async def log_activity(self, event_type: str, description: str, meta: str = None, channel: str = None, person_id: str = None) -> None:
        await _routed_write(
            "log_activity",
            event_type=event_type,
            description=description,
            meta=meta,
            channel=channel,
            person_id=person_id,
        )


class InMemoryTaskStore(TaskStore):
    def __init__(self):
        self.tasks: dict[int, dict] = {}
        self.executions: list[dict] = []
        self.events: list[dict] = []
        self.links: list[dict] = []
        self._next_id = 1

    async def get_task(self, task_id: int) -> dict | None:
        return self.tasks.get(task_id)

    async def create_agent_task(self, type: str, title: str, details: str, assigned_to: str | None, assigned_by: str, priority: str, horizon: str | None, visibility: str) -> dict:
        tid = self._next_id
        self._next_id += 1
        t = {
            "id": tid, "type": type, "title": title, "details": details,
            "assigned_to": assigned_to, "assigned_by": assigned_by,
            "priority": priority, "horizon": horizon, "visibility": visibility,
            "kanban_status": "todo"
        }
        self.tasks[tid] = t
        return t

    async def list_executions(self, task_id: int) -> list[dict]:
        return [e for e in self.executions if e["task_id"] == task_id]

    async def update_unified_task_heartbeat(self, task_id: int) -> None:
        pass

    async def add_task_event(self, task_id: int, event_type: str, actor_id: str | None, payload: dict) -> None:
        self.events.append({"task_id": task_id, "type": event_type, "actor": actor_id, "payload": payload})

    async def start_execution(self, task_id: int, run_id: str) -> None:
        self.executions.append({"task_id": task_id, "run_id": run_id, "status": "running", "logs": ""})
        if task_id in self.tasks:
            self.tasks[task_id]["current_run_id"] = run_id

    async def finish_execution(self, run_id: str, status: str, logs: str, metrics: dict | None = None) -> None:
        for e in self.executions:
            if e["run_id"] == run_id:
                e["status"] = status
                e["logs"] = logs

    async def complete_task(self, task_id: int, completion_note: str = "") -> dict | None:
        if task_id not in self.tasks:
            return None
        self.tasks[task_id]["kanban_status"] = "done"
        self.tasks[task_id]["status"] = "done"
        if completion_note:
            self.tasks[task_id]["completion_note"] = completion_note
        return dict(self.tasks[task_id])

    async def block_task(self, task_id: int, reason: str) -> None:
        if task_id in self.tasks:
            self.tasks[task_id]["kanban_status"] = "blocked"
            self.tasks[task_id]["status"] = "blocked"
            self.tasks[task_id]["error"] = reason

    async def link_tasks(self, parent_id: int, child_id: int) -> bool:
        if parent_id == child_id: return False
        for l in self.links:
            if l["parent_id"] == child_id and l["child_id"] == parent_id:
                return False
        self.links.append({"parent_id": parent_id, "child_id": child_id})
        return True

    async def promote_ready_tasks(self) -> None:
        pass

    async def reassign_task(self, task_id: int, assigned_to: str) -> dict | None:
        if task_id not in self.tasks:
            return None
        self.tasks[task_id]["assigned_to"] = assigned_to
        return dict(self.tasks[task_id])

    async def get_task_links(self, task_id: int) -> dict:
        parents = [l for l in self.links if l["child_id"] == task_id]
        children = [l for l in self.links if l["parent_id"] == task_id]
        return {"parents": parents, "children": children}

    async def list_task_events(self, task_id: int) -> list[dict]:
        return [e for e in self.events if e["task_id"] == task_id]

    def is_system_virtual_task_id(self, task_id: int) -> bool:
        return task_id < 0

    async def get_system_task_detail(self, task_id: int) -> dict | None:
        return None

    async def set_kanban_status(self, task_id: int, status: str) -> dict | None:
        if task_id not in self.tasks:
            return None
        self.tasks[task_id]["kanban_status"] = status
        return dict(self.tasks[task_id])

    async def set_unified_task_running(self, task_id: int) -> None:
        if task_id in self.tasks:
            self.tasks[task_id]["kanban_status"] = "running"

    async def create_task(
        self,
        title: str,
        assigned_to: str,
        assigned_by: str,
        details: str = "",
        due_at: str | None = None,
        requires_approval: bool = False,
        approver_person_id: str | None = None,
        remind_visibility: str = "private",
        remind_channel_id: int | None = None,
        category: str = "Task",
        is_recurring: bool = False,
        in_progress: bool = False,
        priority: str = "normal",
        horizon: str | None = None,
    ) -> dict:
        tid = self._next_id
        self._next_id += 1
        t = {
            "id": tid,
            "title": title,
            "type": "chore",
            "assigned_to": assigned_to,
            "assigned_by": assigned_by,
            "details": details,
            "status": "pending",
            "requires_approval": requires_approval,
            "priority": priority,
            "kanban_status": "running" if in_progress else "todo",
        }
        if due_at is not None:
            t["due_at"] = due_at
        if approver_person_id is not None:
            t["approver_person_id"] = approver_person_id
        if horizon is not None:
            t["horizon"] = horizon
        self.tasks[tid] = t
        return dict(t)

    async def convert_task_type(self, task_id: int, new_type: str, *, assignee: str | None = None) -> dict | None:
        if task_id in self.tasks:
            self.tasks[task_id]["type"] = new_type
            if assignee:
                self.tasks[task_id]["assigned_to"] = assignee
            return self.tasks[task_id]
        return None

    async def update_task(self, task_id: int, updates: dict) -> dict | None:
        if task_id in self.tasks:
            self.tasks[task_id].update(updates)
            return self.tasks[task_id]
        return None

    async def list_tasks_for_person(
        self,
        person_id: str,
        status: str = "all",
        include_assigned_by: bool = True,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]:
        rows = []
        for t in self.tasks.values():
            if t.get("assigned_to") != person_id:
                if not include_assigned_by or t.get("assigned_by") != person_id:
                    continue
            if status != "all" and t.get("status") != status:
                continue
            rows.append(dict(t))
        return _paginate_rows(rows, limit=limit, offset=offset)

    async def list_all_tasks(
        self,
        status: str = "all",
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]:
        if status == "all":
            rows = [dict(t) for t in self.tasks.values()]
        else:
            rows = [dict(t) for t in self.tasks.values() if t.get("status") == status]
        return _paginate_rows(rows, limit=limit, offset=offset)

    async def snooze_task(self, task_id: int, snooze_until: str) -> dict | None:
        if task_id in self.tasks:
            self.tasks[task_id]["snooze_until"] = snooze_until
            self.tasks[task_id]["snooze_count"] = int(self.tasks[task_id].get("snooze_count") or 0) + 1
            return dict(self.tasks[task_id])
        return None

    async def approve_task(self, task_id: int, approved: bool) -> dict | None:
        if task_id in self.tasks:
            self.tasks[task_id]["status"] = "approved" if approved else "pending"
            return self.tasks[task_id]
        return None

    async def delete_task(self, task_id: int) -> None:
        if task_id in self.tasks:
            del self.tasks[task_id]

    async def store_message_mapping(self, message_id: int, event_id: str, event_title: str = None, message_type: str = "event") -> None:
        pass

    async def log_activity(self, event_type: str, description: str, meta: str = None, channel: str = None, person_id: str = None) -> None:
        pass
