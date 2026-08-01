"""API routes: tasks (family-bot-8lx.2 hard-cut)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, Header
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Optional
import asyncio
import logging
import os
import time
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import api.common as _ac
from config import config as _config_check  # noqa: F401 — _ac.config from common
from presence_service import presence_service
from ha_service import ha_service as ha_service_mod
from weather_service import get_weather
from frigate_service import frigate_service
from garbage_service import get_next_collections, get_tomorrow_collection
from constants import registry as person_registry, PERSON_IDS, PERSON_DISPLAY
from constants import HA_SUPPORT_BRIGHTNESS, HA_SUPPORT_COLOR_TEMP, HA_SUPPORT_RGB_COLOR, HA_RGB_MODES, HA_DIM_MODES
from utils.discord_helpers import next_automation_run
from recommendation_engine import get_recommendations
import summary_builder as summary_builder_mod
from llm.chat import chat_general
import auth_service
import secrets
import ipaddress

log = logging.getLogger(__name__)


def build_tasks_router(ctx: Any) -> APIRouter:
    """Register tasks routes; closes over container services via ctx."""
    router = APIRouter()
    import db_writes
    bot = ctx.bot
    container = ctx.container
    db = ctx.db
    _frigate = ctx.frigate
    notification_dispatcher = ctx.notification_dispatcher
    calendar_service = ctx.calendar_service
    weather_module = ctx.weather_module
    ha_service = ctx.ha_service
    summary_builder = ctx.summary_builder
    connection_manager = ctx.connection_manager
    supervisor = ctx.supervisor
    task_store = ctx.task_store
    unified_tasks = ctx.unified_tasks
    http_session = ctx.http_session
    # login_attempts shared on ctx for auth
    if not hasattr(ctx, "login_attempts"):
        ctx.login_attempts = {}
    login_attempts = ctx.login_attempts

    async def _complete_with_side_effects(task_id: int, task: dict, user: _ac.Person, note: str = "") -> dict:
        """Mark done + approval DM/auto-approve + dependency promotion. Shared by /complete and /move."""
        if not unified_tasks:
            raise HTTPException(status_code=503, detail="Unified task service not available")
        from services.unified_task_service import TaskValidationError
        try:
            return await unified_tasks.complete_task(task_id, actor_id=user.id, note=note, via="api")
        except TaskValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/api/tasks", dependencies=[Depends(_ac.verify_token)])
    async def get_tasks(
        status: str = "all",
        all_people: bool = False,
        limit: int | None = Query(None),
        offset: int = Query(0, ge=0),
        user: _ac.Person = Depends(_ac.verify_token),
    ):
        valid = {"all", "pending", "done", "approved"}
        if status not in valid:
            raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(sorted(valid))}")

        try:
            # Admins and parents can request all tasks; everyone else sees only their own
            if all_people and user.role in {"admin", "parents"}:
                rows = await task_store.list_all_tasks(status=status, limit=limit, offset=offset)
            else:
                rows = await task_store.list_tasks_for_person(
                    user.id, status=status, include_assigned_by=True, limit=limit, offset=offset
                )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        from task_access import person_matches, registry_person_id
        result = []
        for t in rows:
            assigned_to = t.get("assigned_to", "")
            assigned_by = t.get("assigned_by", "")
            result.append({
                **t,
                "assigned_to_name": person_registry.display_name(registry_person_id(assigned_to) or assigned_to),
                "assigned_by_name": person_registry.display_name(registry_person_id(assigned_by) or assigned_by),
                "can_complete": t.get("status") == "pending" and (
                    person_matches(assigned_to, user.id) or user.role in {"admin", "parents"}),
                "can_approve": t.get("status") == "done" and (
                    person_matches(assigned_by, user.id) or user.role in {"admin", "parents"}),
            })
        if user.role in {"admin", "parents"}:
            try:
                result.extend(await db.list_cognitive_tasks_as_system(limit=15))
            except Exception:
                log.warning("get_tasks: system projection failed (non-fatal)", exc_info=True)
        return result

    @router.post("/api/tasks", dependencies=[Depends(_ac.verify_token)])
    async def create_task(data: Dict[str, Any], user: _ac.Person = Depends(_ac.verify_token)):
        if not unified_tasks:
            raise HTTPException(status_code=503, detail="Unified task service not available")

        req_type = str(data.get("type", "chore")).strip() or "chore"
        if req_type != "chore":
            raise HTTPException(status_code=400,
                detail="The /api/tasks POST creates chores only; use the kanban_create tool for research/bernie/code tasks.")

        raw_assigned_to = str(data.get("assigned_to", user.id)).strip() or user.id
        from task_access import person_matches, registry_person_id
        assigned_to = person_registry.resolve(raw_assigned_to) or registry_person_id(raw_assigned_to) or raw_assigned_to
        if not person_matches(assigned_to, user.id) and user.role not in {"admin", "parents"}:
            raise HTTPException(status_code=403, detail="You can only assign tasks to yourself")

        if not person_registry.get(assigned_to):
            raise HTTPException(status_code=404, detail="Assigned person not found")

        title = str(data.get("title", "")).strip()
        if not title:
            raise HTTPException(status_code=400, detail="Missing task title")

        priority = str(data.get("priority") or "normal").strip().lower()
        if priority not in {"low", "normal", "high"}:
            raise HTTPException(status_code=400, detail="priority must be low|normal|high")

        remind_visibility = str(data.get("remind_visibility", "private")).strip().lower() or "private"
        if remind_visibility not in {"private", "channel"}:
            raise HTTPException(status_code=400, detail="remind_visibility must be 'private' or 'channel'")

        due_at = data.get("due_at")
        if due_at:
            try:
                datetime.fromisoformat(str(due_at))
            except Exception:
                raise HTTPException(status_code=400, detail="due_at must be an ISO datetime")

        from services.unified_task_service import TaskValidationError
        try:
            task = await unified_tasks.create_chore_task(
                title=title,
                details=str(data.get("details", "")).strip(),
                assigned_to=assigned_to,
                assigned_by=user.id,
                due_at=str(due_at) if due_at else None,
                priority=priority,
                category=str(data.get("category") or "Task").strip(),
                horizon=(str(data["horizon"]).strip() if data.get("horizon") else None),
                remind_visibility=remind_visibility,
                remind_channel_id=_ac.config.get("schedule_channel_id") if remind_visibility == "channel" else None,
                is_recurring=bool(data.get("is_recurring", False)),
                in_progress=bool(data.get("in_progress", False)),
            )
        except TaskValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

        await connection_manager.broadcast({"type": "task.update", "action": "created", "task_id": task["id"]})
        return task

    @router.post("/api/tasks/{task_id}/complete", dependencies=[Depends(_ac.verify_token)])
    async def complete_task(task_id: int, data: Dict[str, Any], user: _ac.Person = Depends(_ac.verify_token)):
        if not unified_tasks:
            raise HTTPException(status_code=503, detail="Unified task service not available")
        if task_store.is_system_virtual_task_id(task_id):
            raise HTTPException(status_code=400, detail="System tasks cannot be modified")
        task = await task_store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.get("status") != "pending":
            raise HTTPException(status_code=400, detail="Task is not pending")
        from task_access import person_matches
        if not person_matches(task.get("assigned_to"), user.id) and user.role not in {"admin", "parents"}:
            raise HTTPException(status_code=403, detail="Only the assignee or a parent can complete this task")

        note = str(data.get("note", "")).strip()
        from services.unified_task_service import TaskValidationError
        try:
            updated = await unified_tasks.complete_task(task_id, actor_id=user.id, note=note, via="api")
        except TaskValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

        await connection_manager.broadcast({"type": "task.update", "action": "completed", "task_id": task_id})
        return updated

    @router.post("/api/tasks/{task_id}/move", dependencies=[Depends(_ac.verify_token)])
    async def move_task_api(task_id: int, data: Dict[str, Any], user: _ac.Person = Depends(_ac.verify_token)):
        if not unified_tasks:
            raise HTTPException(status_code=503, detail="Unified task service not available")
        if task_store.is_system_virtual_task_id(task_id):
            raise HTTPException(status_code=400, detail="System tasks cannot be modified")
        task = await task_store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        from task_access import person_matches
        if not person_matches(task.get("assigned_to"), user.id) and user.role not in {"admin", "parents"}:
            raise HTTPException(status_code=403, detail="You can only move your own tasks")
        target = str(data.get("status", "")).strip()

        from services.unified_task_service import TaskValidationError
        broadcast_action = "moved"
        try:
            result = await unified_tasks.move_task(
                task_id,
                target,
                actor_id=user.id,
                reason=str(data.get("reason", "")).strip(),
                via="board",
            )
        except TaskValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

        if target == "done":
            broadcast_action = "completed"

        await connection_manager.broadcast(
            {"type": "task.update", "action": broadcast_action, "task_id": task_id, "status": target})
        return result

    @router.post("/api/tasks/agent", dependencies=[Depends(_ac.verify_token)])
    async def create_agent_task_api(data: Dict[str, Any], user: _ac.Person = Depends(_ac.verify_token)):
        if user.role not in {"admin", "parents"}:
            raise HTTPException(status_code=403, detail="Only parents/admin create agent tasks")
        ttype = str(data.get("type", "")).strip()
        if ttype not in {"research", "bernie", "code"}:
            raise HTTPException(status_code=400, detail="type must be research|bernie|code")
        assignee = data.get("assigned_to") or None
        if not str(data.get("title", "")).strip():
            raise HTTPException(status_code=400, detail="title is required")
        priority = str(data.get("priority", "normal")).strip().lower()
        if priority not in {"low", "normal", "high"}:
            raise HTTPException(status_code=400, detail="priority must be low|normal|high")

        if not unified_tasks:
            raise HTTPException(status_code=503, detail="Task service unavailable")

        from services.unified_task_service import TaskValidationError
        try:
            t = await unified_tasks.create_agent_task(
                task_type=ttype,
                title=str(data["title"]),
                details=str(data.get("details", "")),
                assigned_to=assignee,
                assigned_by=user.id,
                priority=priority,
                horizon=data.get("horizon"),
            )
        except TaskValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

        return t

    @router.get("/api/tasks/{task_id}/detail", dependencies=[Depends(_ac.verify_token)])
    async def task_detail_api(task_id: int, user: _ac.Person = Depends(_ac.verify_token)):
        if task_store.is_system_virtual_task_id(task_id):
            if user.role not in {"admin", "parents"}:
                raise HTTPException(status_code=403, detail="Not permitted")
            detail = await task_store.get_system_task_detail(task_id)
            if not detail:
                raise HTTPException(status_code=404, detail="Task not found")
            return detail
        task = await task_store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        from task_access import can_view_task
        if not can_view_task(task, user.id, user.role):
            raise HTTPException(status_code=403, detail="Not permitted")
        links = await task_store.get_task_links(task_id)
        return {
            "task": task,
            "runs": await task_store.list_executions(task_id),
            "events": await task_store.list_task_events(task_id),
            **links,  # spreads "parents" and "children" keys
        }

    @router.post("/api/tasks/{task_id}/comment", dependencies=[Depends(_ac.verify_token)])
    async def comment_task_api(task_id: int, data: Dict[str, Any], user: _ac.Person = Depends(_ac.verify_token)):
        if not unified_tasks:
            raise HTTPException(status_code=503, detail="Unified task service not available")
        if task_store.is_system_virtual_task_id(task_id):
            raise HTTPException(status_code=403, detail="Comments are not allowed on system tasks")
        text = str(data.get("text", "")).strip()
        if not text:
            raise HTTPException(status_code=400, detail="Comment text is required")
        task = await task_store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        from task_access import can_view_task
        if not can_view_task(task, user.id, user.role):
            raise HTTPException(status_code=403, detail="Not permitted")
        from services.unified_task_service import TaskValidationError
        try:
            await unified_tasks.add_comment(task_id, actor_id=user.id, text=text)
        except TaskValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @router.post("/api/tasks/{task_id}/snooze", dependencies=[Depends(_ac.verify_token)])
    async def snooze_task(task_id: int, data: Dict[str, Any], user: _ac.Person = Depends(_ac.verify_token)):
        if not unified_tasks:
            raise HTTPException(status_code=503, detail="Unified task service not available")
        if task_store.is_system_virtual_task_id(task_id):
            raise HTTPException(status_code=400, detail="System tasks cannot be modified")
        task = await task_store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        from task_access import person_matches
        if not person_matches(task.get("assigned_to"), user.id) and user.role not in {"admin", "parents"}:
            raise HTTPException(status_code=403, detail="Only the assignee or a parent can snooze this task")

        snooze_until = str(data.get("snooze_until", "")).strip()
        if not snooze_until:
            raise HTTPException(status_code=400, detail="snooze_until is required")

        from services.unified_task_service import TaskValidationError
        try:
            return await unified_tasks.snooze_task(
                task_id,
                actor_id=user.id,
                snooze_until=snooze_until,
            )
        except TaskValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/api/tasks/{task_id}/approve", dependencies=[Depends(_ac.verify_token)])
    async def approve_task(task_id: int, data: Dict[str, Any], user: _ac.Person = Depends(_ac.verify_token)):
        if not unified_tasks:
            raise HTTPException(status_code=503, detail="Unified task service not available")
        if task_store.is_system_virtual_task_id(task_id):
            raise HTTPException(status_code=400, detail="System tasks cannot be modified")
        approved = bool(data.get("approved", False))
        task = await task_store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        from task_access import person_matches
        approver_person_id = task.get("approver_person_id") or task.get("assigned_by")
        if not person_matches(approver_person_id, user.id) and user.role != "admin":
            raise HTTPException(status_code=403, detail="Only the assigner can approve this task")

        from services.unified_task_service import TaskValidationError
        try:
            return await unified_tasks.approve_task(
                task_id,
                actor_id=user.id,
                approved=approved,
            )
        except TaskValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/api/tasks/{task_id}/convert", dependencies=[Depends(_ac.verify_token)])
    async def convert_task_api(task_id: int, data: Dict[str, Any], user: _ac.Person = Depends(_ac.verify_token)):
        if not unified_tasks:
            raise HTTPException(status_code=503, detail="Unified task service not available")
        if task_store.is_system_virtual_task_id(task_id):
            raise HTTPException(status_code=400, detail="System tasks cannot be modified")
        if user.role not in {"admin", "parents"}:
            raise HTTPException(status_code=403, detail="Only parents/admin can change task type")
        task = await task_store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.get("type") == "system":
            raise HTTPException(status_code=400, detail="System tasks cannot be converted")

        assignee = data.get("assigned_to")
        if assignee is not None:
            assignee = str(assignee).strip() or None

        from services.unified_task_service import TaskValidationError
        try:
            updated = await unified_tasks.convert_task(
                task_id,
                actor_id=user.id,
                new_type=str(data.get("type", "")).strip(),
                assignee=assignee,
                enqueue=bool(data.get("enqueue", True)),
            )
        except TaskValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

        await connection_manager.broadcast({"type": "task.update", "action": "converted", "task_id": task_id})
        return updated

    @router.patch("/api/tasks/{task_id}", dependencies=[Depends(_ac.verify_token)])
    async def update_task_api(task_id: int, data: Dict[str, Any], user: _ac.Person = Depends(_ac.verify_token)):
        if not unified_tasks:
            raise HTTPException(status_code=503, detail="Unified task service not available")
        if task_store.is_system_virtual_task_id(task_id):
            raise HTTPException(status_code=400, detail="System tasks cannot be modified")
        task = await task_store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        from task_access import person_matches
        if not person_matches(task.get("assigned_to"), user.id) and user.role not in {"admin", "parents"}:
            raise HTTPException(status_code=403, detail="You can only update your own tasks")

        from services.unified_task_service import TaskValidationError
        try:
            updated = await unified_tasks.update_task(task_id, actor_id=user.id, updates=data)
        except TaskValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

        await connection_manager.broadcast({"type": "task.update", "action": "updated", "task_id": task_id})
        return updated

    @router.delete("/api/tasks/{task_id}", dependencies=[Depends(_ac.verify_token)])
    async def delete_task_api(task_id: int, user: _ac.Person = Depends(_ac.verify_token)):
        if not unified_tasks:
            raise HTTPException(status_code=503, detail="Unified task service not available")
        if task_store.is_system_virtual_task_id(task_id):
            raise HTTPException(status_code=400, detail="System tasks cannot be modified")
        task = await task_store.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        from task_access import person_matches
        if not person_matches(task.get("assigned_to"), user.id) and user.role not in {"admin", "parents"}:
            raise HTTPException(status_code=403, detail="Only the assignee or a parent can delete this task")

        from services.unified_task_service import TaskValidationError
        try:
            await unified_tasks.delete_task(task_id, actor_id=user.id)
        except TaskValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

        await connection_manager.broadcast({"type": "task.update", "action": "deleted", "task_id": task_id})
        return {"ok": True}

    @router.get("/api/automations", dependencies=[Depends(_ac.verify_token)])
    async def get_automations(
        limit: int | None = Query(None),
        offset: int = Query(0, ge=0),
        user: _ac.Person = Depends(_ac.verify_token),
    ):
        try:
            if user.role == "admin":
                rows = await db.list_all_automations(limit=limit, offset=offset)
            else:
                rows = await db.list_automations_for_person(
                    user.id, include_created_by=True, limit=limit, offset=offset
                )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        for row in rows:
            row["person_name"] = person_registry.display_name(row.get("person_id", ""))
            row["created_by_name"] = person_registry.display_name(row.get("created_by", ""))
        return rows

    @router.post("/api/automations", dependencies=[Depends(_ac.verify_token)])
    async def create_automation(data: Dict[str, Any], user: _ac.Person = Depends(_ac.verify_token)):
        title = str(data.get("title", "")).strip()
        message = str(data.get("message", "")).strip()
        if not title or not message:
            raise HTTPException(status_code=400, detail="title and message are required")

        person_id = str(data.get("person_id", user.id)).strip() or user.id
        if person_id != user.id and user.role not in {"admin", "parents"}:
            raise HTTPException(status_code=403, detail="You can only create automations for yourself")
        if not person_registry.get(person_id):
            raise HTTPException(status_code=404, detail="_ac.Person not found")

        schedule_kind = str(data.get("schedule_kind", "weekly")).strip().lower()
        valid_schedule_kinds = {"cron", "daily", "weekly", "hourly", "once"}
        if schedule_kind not in valid_schedule_kinds:
            raise HTTPException(status_code=400, detail=f"schedule_kind must be one of: {', '.join(sorted(valid_schedule_kinds))}")

        schedule_payload = data.get("schedule_payload") or {}
        if not isinstance(schedule_payload, dict):
            raise HTTPException(status_code=400, detail="schedule_payload must be an object")

        audience_scope = str(data.get("audience_scope", "self")).strip().lower()
        if audience_scope not in {"self", "everyone"}:
            raise HTTPException(status_code=400, detail="audience_scope must be 'self' or 'everyone'")

        next_run_at = data.get("next_run_at")
        supplied_next_run_at = None
        if next_run_at:
            try:
                supplied_next_run_at = datetime.fromisoformat(str(next_run_at))
            except Exception:
                raise HTTPException(status_code=400, detail="next_run_at must be an ISO datetime")

        tz_name = str(data.get("timezone", _ac.config.get("timezone", "America/Halifax"))).strip() or "America/Halifax"
        try:
            ZoneInfo(tz_name)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid timezone")

        try:
            computed_next_run = next_automation_run(schedule_kind, schedule_payload, tz_name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid schedule payload: {e}")

        if computed_next_run is None:
            raise HTTPException(status_code=400, detail="Schedule does not produce a future run time")

        if supplied_next_run_at is not None and supplied_next_run_at.tzinfo is None:
            supplied_next_run_at = supplied_next_run_at.replace(tzinfo=ZoneInfo(tz_name))

        next_run_iso = (supplied_next_run_at or computed_next_run).isoformat()

        created = await db_writes.create_automation(
            title=title,
            message=message,
            person_id=person_id,
            schedule_kind=schedule_kind,
            schedule_payload=schedule_payload,
            timezone=tz_name,
            created_by=user.id,
            audience_scope=audience_scope,
            next_run_at=next_run_iso,
        )
        created["person_name"] = person_registry.display_name(created.get("person_id", ""))
        created["created_by_name"] = person_registry.display_name(created.get("created_by", ""))
        return created

    @router.patch("/api/automations/{automation_id}", dependencies=[Depends(_ac.verify_token)])
    async def patch_automation(automation_id: int, data: Dict[str, Any], user: _ac.Person = Depends(_ac.verify_token)):
        row = await db.get_automation(automation_id)
        if not row:
            raise HTTPException(status_code=404, detail="Automation not found")
        if user.role != "admin" and user.id not in {row.get("person_id"), row.get("created_by")}:
            raise HTTPException(status_code=403, detail="Not allowed")

        if "is_active" in data:
            row = await db_writes.set_automation_active(automation_id, bool(data.get("is_active")))
        return row

    @router.delete("/api/automations/{automation_id}", dependencies=[Depends(_ac.verify_token)])
    async def remove_automation(automation_id: int, user: _ac.Person = Depends(_ac.verify_token)):
        row = await db.get_automation(automation_id)
        if not row:
            raise HTTPException(status_code=404, detail="Automation not found")
        if user.role != "admin" and user.id not in {row.get("person_id"), row.get("created_by")}:
            raise HTTPException(status_code=403, detail="Not allowed")
        await db_writes.delete_automation(automation_id)
        return {"ok": True}


    return router
