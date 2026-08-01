"""Unified Task Mutation Facade — Phase 3.3.

Provides a single mutation facade that all write paths (board, chat tools, API, bridge)
call to avoid logic drift. See:
.planning/architectural_hardening_plan-UNIFIED-2026-05-31.md
"""
import logging
from typing import Any
import db_writes

log = logging.getLogger(__name__)

_AGENT_TASK_TYPES = frozenset({"research", "bernie", "code"})


class TaskValidationError(ValueError):
    """Exception raised when task validation rules are violated."""
    pass


class UnifiedTaskService:
    """Facade for all unified task write and mutation operations."""

    def __init__(
        self,
        task_store: Any,
        person_registry: Any = None,
        config: dict | None = None,
        notification_router: Any = None,
    ):
        self.task_store = task_store
        self.config = config or {}
        self.notification_router = notification_router

        if person_registry is None:
            from constants import registry as default_registry
            self.person_registry = default_registry
        else:
            self.person_registry = person_registry

    def _normalize_assignee(self, raw: str | None, *, task_type: str) -> str | None:
        """Normalize assignee storage: agent tasks use person:{id} for family members."""
        if raw is None:
            return None
        s = str(raw).strip()
        if not s:
            return None
        if s.startswith("agent:"):
            return s
        from task_access import registry_person_id

        bare = s[7:] if s.startswith("person:") else s
        canon = self.person_registry.resolve(bare) or registry_person_id(bare) or s
        if task_type in _AGENT_TASK_TYPES and canon and not str(canon).startswith("agent:"):
            return f"person:{canon}"
        return canon

    def _default_assignee_for_type(self, task_type: str) -> str | None:
        """First agent:* entry from config.task_types for convert_task fallbacks."""
        for entry in (self.config.get("task_types") or {}).get(task_type) or []:
            s = str(entry)
            if s.startswith("agent:"):
                return s
        return None

    async def create_agent_task(
        self,
        *,
        task_type: str,          # research | bernie | code
        title: str,
        details: str = "",
        assigned_to: str | None = None,
        assigned_by: str,
        priority: str = "normal",
        horizon: str | None = None,
    ) -> dict:
        """Single path for agent task creation:

        - validates assignee using validate_assignment
        - sets visibility (internal for code, family otherwise)
        - writes to task_store (create_agent_task)
        - if research: enqueues to research_bridge
        - returns the final task dict (re-fetched after enqueue if needed)
        """
        from task_types import validate_assignment

        title = str(title or "").strip()
        if not title:
            raise TaskValidationError("title is required")

        if task_type not in {"research", "bernie", "code"}:
            raise TaskValidationError("type must be research|bernie|code")

        canonical_assigned_to = self._normalize_assignee(assigned_to, task_type=task_type)
        canonical_assigned_by = self._normalize_assignee(assigned_by, task_type=task_type)
        if canonical_assigned_by is None and assigned_by:
            canonical_assigned_by = str(assigned_by).strip()

        # Validate assignment
        if not validate_assignment(task_type, canonical_assigned_to, self.config):
            raise TaskValidationError(
                f"Assignee '{assigned_to}' is not permitted for task type '{task_type}' (config.task_types)."
            )

        # Set visibility: internal for code, family otherwise
        visibility = "internal" if task_type == "code" else "family"

        # Database writes go through task_store only
        t = await self.task_store.create_agent_task(
            type=task_type,
            title=title,
            details=details,
            assigned_to=canonical_assigned_to,
            assigned_by=canonical_assigned_by,
            priority=priority,
            horizon=horizon,
            visibility=visibility,
        )

        if task_type == "research":
            await self.enqueue_research_run(t["id"], title, actor_id=canonical_assigned_by)
            # Re-fetch the final task dict after research enqueuing
            t = await self.task_store.get_task(t["id"])

        return t

    async def complete_task(
        self,
        task_id: int,
        *,
        actor_id: str,
        note: str = "",
        via: str = "api",
    ) -> dict:
        """Unifies task completion across all surfaces."""
        task = await self.task_store.get_task(task_id)
        if not task:
            raise TaskValidationError("Task not found")

        from task_access import registry_person_id

        if task.get("type") == "chore":
            if task.get("status") != "pending":
                raise TaskValidationError("Task is not pending")

            updated = await self.task_store.complete_task(task_id, completion_note=note)
            await self.task_store.add_task_event(task_id, "completed", actor_id, {"note": note})

            if bool(task.get("requires_approval")):
                assigner_id = None
                assigned_by = task.get("assigned_by")
                if assigned_by:
                    canon_assigned_by = self.person_registry.resolve(registry_person_id(assigned_by))
                    assigner = self.person_registry.get(canon_assigned_by or "") or {}
                    assigner_id = assigner.get("discord_id")

                if assigner_id and self.notification_router:
                    embed_text = f"{self.person_registry.display_name(actor_id)} marked task #{task_id} complete: **{task['title']}**"
                    if note:
                        embed_text += f"\nNote: {note}"
                    msg = await self.notification_router.notify(self.notification_router.notification(
                        recipient_id=str(assigner_id),
                        message=embed_text + "\nReact ✅ to approve or ❌ to reopen.",
                    ))
                    dm_msg = msg.get("discord")
                    if dm_msg and hasattr(dm_msg, "id"):
                        try:
                            await dm_msg.add_reaction("✅")
                            await dm_msg.add_reaction("❌")
                        except Exception:
                            pass
                        await self.task_store.store_message_mapping(
                            dm_msg.id, f"task:{task_id}", task.get("title"), message_type="task_approval"
                        )
            else:
                updated = await self.task_store.approve_task(task_id, approved=True)
                await self.task_store.add_task_event(task_id, "approved", actor_id, {"auto": True})

            await self.task_store.log_activity("task", f"Completed <b>{task['title']}</b>", "Awaiting approval", "Discord", person_id=actor_id)
            await self.task_store.promote_ready_tasks()
            return updated or task
        else:
            return await self._complete_agent_task(task_id, actor_id=actor_id, note=note, via=via)

    async def _complete_agent_task(
        self,
        task_id: int,
        *,
        actor_id: str,
        note: str,
        via: str,
    ) -> dict:
        """Agent/research/bernie/code: execution row + complete (all surfaces)."""
        import time as _t

        run_id = f"{via}-{task_id}-{int(_t.time())}"
        await self.task_store.start_execution(task_id, run_id)
        await self.task_store.finish_execution(run_id, status="completed", logs=note or "")
        updated = await self.task_store.complete_task(task_id, completion_note=note)
        await self.task_store.add_task_event(task_id, "completed", actor_id, {"via": via})
        await self.task_store.promote_ready_tasks()
        return updated or await self.task_store.get_task(task_id) or {}

    async def _send_blocked_ping(self, task_id: int, task: dict, reason: str) -> None:
        if not self.notification_router:
            return
        msg = f"⚠ Task #{task_id} blocked: {reason or 'moved via board'}"
        try:
            from notify_targets import blocked_ping_recipient
            from task_access import person_to_discord_id

            recipient = blocked_ping_recipient(task)
            if recipient:
                did = person_to_discord_id(recipient)
                if did:
                    await self.notification_router.notify(self.notification_router.notification(
                        recipient_id=str(did),
                        message=msg,
                    ))
                    return
            anvil_id = self.config.get("anvil_channel_id")
            if anvil_id:
                await self.notification_router.notify(self.notification_router.notification(
                    recipient_id=str(anvil_id),
                    message=msg,
                ))
        except Exception:
            log.warning("blocked-ping failed for #%s (non-fatal)", task_id, exc_info=True)

    async def move_task(
        self,
        task_id: int,
        status: str,
        *,
        actor_id: str,
        reason: str = "",
        via: str = "api",
    ) -> dict:
        """Move task status and handle side effects (completing, blocking with pings)."""
        task = await self.task_store.get_task(task_id)
        if not task:
            raise TaskValidationError("Task not found")

        from task_status import UNIFIED_STATUSES

        status = str(status).strip()

        if status == "done":
            if task.get("type") == "chore":
                if task.get("status") != "pending":
                    raise TaskValidationError("Task is not pending")
                return await self.complete_task(task_id, actor_id=actor_id, note=reason, via=via or "board")
            return await self._complete_agent_task(
                task_id, actor_id=actor_id, note=reason, via=via or "board",
            )

        elif status == "blocked":
            await self.task_store.block_task(task_id, reason or "moved via board")
            updated = await self.task_store.get_task(task_id)
            await self.task_store.add_task_event(task_id, "blocked", actor_id, {"reason": reason})
            await self._send_blocked_ping(task_id, task, reason)
            return updated or task

        elif status in UNIFIED_STATUSES:
            updated = await self.task_store.set_kanban_status(task_id, status)
            return updated or task
        else:
            raise TaskValidationError(f"invalid status '{status}'")

    async def create_chore_task(
        self,
        *,
        title: str,
        details: str = "",
        assigned_to: str,
        assigned_by: str,
        due_at: str | None = None,
        priority: str = "normal",
        category: str = "Task",
        horizon: str | None = None,
        remind_visibility: str = "private",
        remind_channel_id: str | int | None = None,
        is_recurring: bool = False,
        in_progress: bool = False,
    ) -> dict:
        """Single path for chore task creation."""
        from task_access import registry_person_id
        from task_types import validate_assignment

        title = str(title or "").strip()
        if not title:
            raise TaskValidationError("title is required")

        priority = str(priority or "normal").strip().lower()
        if priority not in {"low", "normal", "high"}:
            raise TaskValidationError("priority must be low|normal|high")

        remind_visibility = str(remind_visibility or "private").strip().lower()
        if remind_visibility not in {"private", "channel"}:
            raise TaskValidationError("remind_visibility must be 'private' or 'channel'")

        if due_at:
            try:
                from datetime import datetime
                datetime.fromisoformat(str(due_at))
            except Exception:
                raise TaskValidationError("due_at must be an ISO datetime")

        assigned_to_canon = self.person_registry.resolve(assigned_to) or registry_person_id(assigned_to) or assigned_to
        assigned_by_canon = self.person_registry.resolve(assigned_by) or registry_person_id(assigned_by) or assigned_by

        if not self.person_registry.get(assigned_to_canon):
            raise TaskValidationError("Assigned person not found")

        if not validate_assignment("chore", assigned_to_canon, self.config):
            raise TaskValidationError(
                f"Assignee '{assigned_to_canon}' is not permitted for a chore (see config.task_types)."
            )

        # Match legacy POST /api/tasks: parent/admin assigning to someone else
        creator_rec = self.person_registry.get(assigned_by_canon) or {}
        creator_role = creator_rec.get("role", "")
        requires_approval = (
            assigned_to_canon != assigned_by_canon
            and creator_role in {"admin", "parents"}
        )
        approver_person_id = assigned_by_canon if requires_approval else None

        # Build remind channel ID if visibility is channel
        final_remind_channel_id = None
        if remind_visibility == "channel":
            final_remind_channel_id = remind_channel_id or self.config.get("schedule_channel_id")
            if final_remind_channel_id:
                final_remind_channel_id = int(final_remind_channel_id)

        task = await self.task_store.create_task(
            title=title,
            details=details,
            assigned_to=assigned_to_canon,
            assigned_by=assigned_by_canon,
            due_at=str(due_at) if due_at else None,
            requires_approval=requires_approval,
            approver_person_id=approver_person_id,
            remind_visibility=remind_visibility,
            remind_channel_id=final_remind_channel_id,
            category=category,
            is_recurring=is_recurring,
            in_progress=in_progress,
            priority=priority,
            horizon=horizon,
        )

        # Best-effort notification
        if self.notification_router:
            try:
                from task_access import person_to_discord_id
                did = person_to_discord_id(assigned_to_canon)
                if did:
                    due_text = ""
                    if due_at:
                        try:
                            from datetime import datetime
                            due_text = f"\nDue: {datetime.fromisoformat(str(due_at)).strftime('%a %b %-d %-I:%M %p')}"
                        except Exception:
                            due_text = f"\nDue: {due_at}"
                    
                    creator_name = self.person_registry.display_name(assigned_by_canon) if hasattr(self.person_registry, 'display_name') else assigned_by_canon.capitalize()
                    
                    message_text = (
                        f"🧹 New task from {creator_name}: **{title}**"
                        f"{due_text}\nUse `/task_done {task['id']}` when complete."
                    )
                    
                    await self.notification_router.notify(
                        self.notification_router.notification(
                            recipient_id=str(did),
                            message=message_text,
                        )
                    )
            except Exception as e:
                log.warning("Chore creation notification failed (non-fatal): %s", e)

        await self.task_store.log_activity(
            "task",
            f"Assigned <b>{title}</b>",
            f"to {self.person_registry.display_name(assigned_to_canon)}",
            "Discord",
            person_id=assigned_by_canon,
        )

        return task

    async def approve_task(
        self,
        task_id: int,
        *,
        actor_id: str,
        approved: bool,
    ) -> dict:
        """Approve or reopen a chore awaiting approval; notifies assignee."""
        task = await self.task_store.get_task(task_id)
        if not task:
            raise TaskValidationError("Task not found")
        if task.get("status") != "done":
            raise TaskValidationError("Task is not awaiting approval")

        updated = await self.task_store.approve_task(task_id, approved=approved)
        if not updated:
            raise TaskValidationError("Task not found or not awaiting approval")
        await self.task_store.add_task_event(
            task_id,
            "approved" if approved else "reopened",
            actor_id,
            {"approved": approved},
        )
        if approved:
            await self.task_store.promote_ready_tasks()

        if self.notification_router:
            try:
                from task_access import person_to_discord_id

                assignee_id = person_to_discord_id(task.get("assigned_to"))
                if assignee_id:
                    actor_name = self.person_registry.display_name(actor_id)
                    if approved:
                        msg = (
                            f"✅ Your task #{task_id} (**{task['title']}**) was approved "
                            f"by {actor_name}."
                        )
                    else:
                        msg = (
                            f"↩️ Your task #{task_id} (**{task['title']}**) was sent back "
                            f"by {actor_name}."
                        )
                    await self.notification_router.notify(
                        self.notification_router.notification(
                            recipient_id=str(assignee_id),
                            message=msg,
                        )
                    )
            except Exception:
                log.warning("approve_task: assignee notify failed for #%s (non-fatal)", task_id, exc_info=True)

        return updated

    async def snooze_task(
        self,
        task_id: int,
        *,
        actor_id: str,
        snooze_until: str,
        preset: str | None = None,
    ) -> dict:
        """Snooze a pending task until the given ISO datetime."""
        task = await self.task_store.get_task(task_id)
        if not task:
            raise TaskValidationError("Task not found")
        if task.get("status") != "pending":
            raise TaskValidationError("Task is not pending")

        snooze_until = str(snooze_until or "").strip()
        if not snooze_until:
            raise TaskValidationError("snooze_until is required")
        try:
            from datetime import datetime
            datetime.fromisoformat(snooze_until)
        except Exception:
            raise TaskValidationError("snooze_until must be ISO datetime")

        updated = await self.task_store.snooze_task(task_id, snooze_until)
        if not updated:
            raise TaskValidationError("Task not found")
        payload = {"until": snooze_until}
        if preset:
            payload["preset"] = preset
        await self.task_store.add_task_event(task_id, "snoozed", actor_id, payload)

        threshold = int(self.config.get("task_snooze_escalation_count", 3))
        if (
            int(updated.get("snooze_count") or 0) >= threshold
            and not updated.get("escalated_at")
            and self.notification_router
        ):
            try:
                from task_access import person_to_discord_id

                assigner_did = person_to_discord_id(updated.get("assigned_by"))
                if assigner_did:
                    await self.notification_router.notify(
                        self.notification_router.notification(
                            recipient_id=str(assigner_did),
                            message=(
                                f"FYI: {self.person_registry.display_name(updated.get('assigned_to', ''))} "
                                f"has snoozed task #{task_id} (**{updated.get('title', 'Task')}**) "
                                f"{updated.get('snooze_count')} times."
                            ),
                        )
                    )
                from datetime import datetime, timezone
                from db_binding import get_database

                await db_writes.routed("mark_task_escalated", 
                    task_id, datetime.now(timezone.utc).isoformat(),
                )
                await self.task_store.add_task_event(
                    task_id, "escalated", actor_id, {"reason": "snooze_count"},
                )
            except Exception:
                log.warning("snooze_task: escalation notify failed for #%s (non-fatal)", task_id, exc_info=True)

        return updated

    async def reassign_task(
        self,
        task_id: int,
        *,
        actor_id: str,
        assigned_to: str | None,
    ) -> dict:
        """Reassign a task after task_types gating and identity normalization."""
        from task_types import validate_assignment

        task = await self.task_store.get_task(task_id)
        if not task:
            raise TaskValidationError("Task not found")

        task_type = task.get("type", "chore")
        new_assignee = None
        if assigned_to is not None:
            new_assignee = self._normalize_assignee(
                str(assigned_to).strip() or None,
                task_type=task_type,
            )

        if not validate_assignment(task_type, new_assignee, self.config):
            raise TaskValidationError(
                f"Assignee '{assigned_to}' not permitted for type '{task.get('type')}'."
            )

        updated = await self.task_store.reassign_task(task_id, new_assignee)
        if not updated:
            raise TaskValidationError("Task not found")
        await self.task_store.add_task_event(
            task_id, "reassigned", actor_id, {"assigned_to": new_assignee},
        )
        return updated

    async def add_comment(
        self,
        task_id: int,
        *,
        actor_id: str,
        text: str,
    ) -> dict:
        """Add a comment event on a task."""
        text = str(text or "").strip()
        if not text:
            raise TaskValidationError("Comment text is required")

        task = await self.task_store.get_task(task_id)
        if not task:
            raise TaskValidationError("Task not found")

        await self.task_store.add_task_event(task_id, "comment", actor_id, {"text": text})
        return task

    async def convert_task(
        self,
        task_id: int,
        *,
        actor_id: str,
        new_type: str,
        assignee: str | None = None,
        enqueue: bool = True,
    ) -> dict:
        """Change task type with assignment validation and optional research enqueue."""
        from task_types import validate_assignment

        task = await self.task_store.get_task(task_id)
        if not task:
            raise TaskValidationError("Task not found")
        if task.get("type") == "system":
            raise TaskValidationError("System tasks cannot be converted")

        new_type = str(new_type or "").strip()
        if new_type not in {"chore", "research", "bernie", "code"}:
            raise TaskValidationError("type must be chore|research|bernie|code")

        if assignee is not None:
            assignee = str(assignee).strip() or None
        if not validate_assignment(new_type, assignee or task.get("assigned_to"), self.config):
            if assignee is None:
                assignee = self._default_assignee_for_type(new_type)
            if not validate_assignment(new_type, assignee, self.config):
                raise TaskValidationError(
                    f"Assignee not permitted for type '{new_type}' (see config.task_types)."
                )

        try:
            updated = await self.task_store.convert_task_type(task_id, new_type, assignee=assignee)
        except ValueError as exc:
            raise TaskValidationError(str(exc)) from exc
        if not updated:
            raise TaskValidationError("Task not found")

        await self.task_store.add_task_event(
            task_id,
            "type_changed",
            actor_id,
            {"from": task.get("type"), "to": new_type, "assigned_to": updated.get("assigned_to")},
        )

        if new_type == "research" and enqueue and task.get("type") != "research":
            await self.enqueue_research_run(
                task_id,
                updated.get("title") or task.get("title", ""),
                actor_id=actor_id,
            )
            updated = await self.task_store.get_task(task_id) or updated

        return updated

    async def update_task(
        self,
        task_id: int,
        *,
        actor_id: str,
        updates: dict,
    ) -> dict:
        """Update task fields; reassignment goes through reassign_task."""
        task = await self.task_store.get_task(task_id)
        if not task:
            raise TaskValidationError("Task not found")

        updates = dict(updates or {})
        updated = None

        if "assigned_to" in updates:
            new_assignee = str(updates["assigned_to"]).strip() or None
            updated = await self.reassign_task(
                task_id, actor_id=actor_id, assigned_to=new_assignee,
            )
            updates = {k: v for k, v in updates.items() if k != "assigned_to"}

        if updates:
            updated = await self.task_store.update_task(task_id, updates)
            if not updated:
                raise TaskValidationError("Task not found")
            await self.task_store.add_task_event(
                task_id, "updated", actor_id, {"fields": list(updates.keys())},
            )
        elif updated is None:
            updated = task

        return updated

    async def delete_task(
        self,
        task_id: int,
        *,
        actor_id: str,
    ) -> dict:
        """Permanently delete a task (no delete event — matches API)."""
        task = await self.task_store.get_task(task_id)
        if not task:
            raise TaskValidationError("Task not found")

        await self.task_store.delete_task(task_id)
        return {"ok": True}

    async def enqueue_research_run(
        self,
        unified_task_id: int,
        topic: str,
        *,
        actor_id: str = "",
    ) -> None:
        """Enqueue ResearchWorker for a unified board research task."""
        from db_binding import get_database

        db = get_database()
        prior = await db.list_research_memory(unified_task_id)
        enriched_topic = topic
        memory_block = db.format_research_memory_for_prompt(prior)
        if memory_block:
            enriched_topic = f"{topic}\n\n{memory_block}"
        cid = await db_writes.routed("create_cognitive_task", 
            type="research",
            payload={
                "topic": enriched_topic,
                "depth": 2,
                "unified_task_id": unified_task_id,
                "delivery": "board",
            },
            actor_id=actor_id or "",
            channel_id="",
            priority=5,
        )
        await self.task_store.start_execution(unified_task_id, f"ct-{cid}")
        await self.task_store.set_unified_task_running(unified_task_id)

    async def report_task_not_completed(
        self,
        task_id: int,
        *,
        actor_id: str,
        note: str = "",
    ) -> dict:
        """Assignee reports task not done — task stays on board; assigner is notified."""
        task = await self.task_store.get_task(task_id)
        if not task:
            raise TaskValidationError("Task not found")

        note = str(note or "").strip()
        await self.task_store.add_task_event(
            task_id, "not_completed", actor_id, {"note": note},
        )

        if task.get("assigned_by") and self.notification_router:
            try:
                from task_access import person_to_discord_id

                assigner_did = person_to_discord_id(task["assigned_by"])
                if assigner_did:
                    actor_name = self.person_registry.display_name(actor_id)
                    extra = f"\nReason: {note}" if note else ""
                    await self.notification_router.notify(
                        self.notification_router.notification(
                            recipient_id=str(assigner_did),
                            message=(
                                f"Heads up: {actor_name} marked task #{task_id} "
                                f"(**{task.get('title', 'Task')}**) as not completed.{extra}"
                            ),
                        )
                    )
            except Exception:
                log.warning(
                    "report_task_not_completed: assigner notify failed for #%s (non-fatal)",
                    task_id,
                    exc_info=True,
                )

        return {"ok": True, "task_id": task_id}

    async def decline_task(
        self,
        task_id: int,
        *,
        actor_id: str,
        reason: str,
    ) -> dict:
        """Decline a task: record reason, remove from board, notify assigner."""
        reason = str(reason or "").strip()
        if not reason:
            raise TaskValidationError("reason is required")

        task = await self.task_store.get_task(task_id)
        if not task:
            raise TaskValidationError("Task not found")

        await self.task_store.add_task_event(task_id, "declined", actor_id, {"reason": reason})
        await self.task_store.delete_task(task_id)

        if task.get("assigned_by") and self.notification_router:
            try:
                from task_access import person_to_discord_id

                assigner_did = person_to_discord_id(task["assigned_by"])
                if assigner_did:
                    actor_name = self.person_registry.display_name(actor_id)
                    await self.notification_router.notify(
                        self.notification_router.notification(
                            recipient_id=str(assigner_did),
                            message=(
                                f"❌ {actor_name} declined task: "
                                f"**{task.get('title')}**\nReason: {reason}"
                            ),
                        )
                    )
            except Exception:
                log.warning("decline_task: assigner notify failed for #%s (non-fatal)", task_id, exc_info=True)

        return {"ok": True, "task_id": task_id}

    async def finalize_research_task(
        self,
        unified_task_id: int,
        *,
        ok: bool,
        summary: str,
        run_id: str,
        error: str | None = None,
        logs: str | None = None,
        metrics: dict | None = None,
        deliver: bool = False,
        container=None,
    ) -> None:
        """Finalizes research task by delegating to research_bridge."""
        from research_bridge import finalize_unified_from_research
        await finalize_unified_from_research(
            unified_task_id=unified_task_id,
            ok=ok,
            summary=summary,
            task_store=self.task_store,
            run_id=run_id,
            error=error,
            logs=logs,
            metrics=metrics,
            deliver=deliver,
            notification_router=self.notification_router,
            container=container,
        )

