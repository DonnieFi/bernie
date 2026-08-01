"""Slash commands: tasks (family-bot-1od — real module, no exec)."""
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands


def register(tree: app_commands.CommandTree, m: Any) -> None:
    """Register tasks slash commands. *m* is the bot module (helpers + config)."""
    # Explicit bot bindings (replaces exec + _LiveNS proxy)
    _broadcast_task_update = m._broadcast_task_update
    _discord_to_person_id = m._discord_to_person_id
    _parse_datetime_local = m._parse_datetime_local
    _resolve_person_id = m._resolve_person_id
    _send_ephemeral = m._send_ephemeral
    _snooze_target = m._snooze_target
    _unified_tasks = m._unified_tasks
    config = m.config
    db_writes = m.db_writes
    get_database = m.get_database
    get_person_group = m.get_person_group
    next_automation_run = m.next_automation_run
    person_display_name = m.person_display_name
    weekday_num = m.weekday_num
    import asyncio
    import os
    import re
    from collections import defaultdict
    from datetime import datetime, timedelta, time, timezone
    from zoneinfo import ZoneInfo
    try:
        import aiohttp
    except ImportError:  # pragma: no cover
        aiohttp = None  # type: ignore

    @tree.command(name="task_add", description="Create a personal task or assign one")
    @app_commands.describe(
        title="Task title",
        details="Optional details",
        for_person="Who this task is for (name or alias)",
        due="Due time (YYYY-MM-DD HH:MM local)",
        remind_in_channel="If on, reminders go to #smithy. Default is DM.",
    )
    async def cmd_task_add(
        interaction: discord.Interaction,
        title: str,
        details: str | None = None,
        for_person: str | None = None,
        due: str | None = None,
        remind_in_channel: bool = False,
    ):
        await interaction.response.defer(ephemeral=True)
        actor_person_id = _discord_to_person_id(interaction.user.id)
        if not actor_person_id:
            await _send_ephemeral(interaction, "❌ I couldn't map your Discord account to a family profile.")
            return

        group = get_person_group(interaction.user) or "kids"
        owner_person_id = _resolve_person_id(for_person) if for_person else actor_person_id
        if not owner_person_id:
            await _send_ephemeral(interaction, "❌ I couldn't find that person.")
            return

        if owner_person_id != actor_person_id and group not in {"admin", "parents"}:
            await _send_ephemeral(interaction, "❌ Only parents/admin can assign tasks to others.")
            return

        due_iso = None
        if due:
            try:
                due_iso = _parse_datetime_local(due).isoformat()
            except Exception:
                await _send_ephemeral(interaction, "❌ Invalid due format. Use `YYYY-MM-DD HH:MM`.")
                return

        visibility = "channel" if remind_in_channel else "private"
        channel_id = config.get("schedule_channel_id") if remind_in_channel else None

        svc = _unified_tasks()
        if not svc:
            await _send_ephemeral(interaction, "❌ Task service unavailable.")
            return
        from services.unified_task_service import TaskValidationError
        try:
            task = await svc.create_chore_task(
                title=title.strip(),
                assigned_to=owner_person_id,
                assigned_by=actor_person_id,
                details=(details or "").strip(),
                due_at=due_iso,
                remind_visibility=visibility,
                remind_channel_id=channel_id,
            )
        except TaskValidationError as exc:
            await _send_ephemeral(interaction, f"❌ {exc}")
            return

        await _broadcast_task_update("created", task["id"])

        requires_approval = bool(task.get("requires_approval"))
        msg = f"✅ Task #{task['id']} created for {person_display_name(owner_person_id)}."
        if requires_approval:
            msg += " Completion will require parent approval."
        await _send_ephemeral(interaction, msg)


    @tree.command(name="task_list", description="List your tasks")
    @app_commands.describe(status="Filter by status", include_assigned="Include tasks you assigned to others")
    @app_commands.choices(status=[
        app_commands.Choice(name="all", value="all"),
        app_commands.Choice(name="pending", value="pending"),
        app_commands.Choice(name="done", value="done"),
        app_commands.Choice(name="approved", value="approved"),
    ])
    async def cmd_task_list(interaction: discord.Interaction, status: str = "all", include_assigned: bool = True):
        await interaction.response.defer(ephemeral=True)
        actor_person_id = _discord_to_person_id(interaction.user.id)
        if not actor_person_id:
            await _send_ephemeral(interaction, "❌ I couldn't map your Discord account to a family profile.")
            return

        rows = await get_database().list_tasks_for_person(actor_person_id, status=status, include_assigned_by=include_assigned)
        if not rows:
            await _send_ephemeral(interaction, "No tasks found.")
            return

        lines = []
        for t in rows[:20]:
            assignee = person_display_name(t.get("assigned_to", ""))
            st = t.get("status", "pending")
            due = t.get("due_at")
            due_txt = ""
            if due:
                try:
                    due_txt = f" · due {datetime.fromisoformat(due).strftime('%b %-d %-I:%M %p')}"
                except Exception:
                    due_txt = f" · due {due}"
            lines.append(f"#{t['id']} [{st}] {t['title']} ({assignee}){due_txt}")

        await _send_ephemeral(interaction, "\n".join(lines))


    @tree.command(name="task_done", description="Mark a task as completed")
    @app_commands.describe(task_id="Task ID", note="Optional completion note")
    async def cmd_task_done(interaction: discord.Interaction, task_id: int, note: str | None = None):
        await interaction.response.defer(ephemeral=True)
        actor_person_id = _discord_to_person_id(interaction.user.id)
        if not actor_person_id:
            await _send_ephemeral(interaction, "❌ I couldn't map your Discord account to a family profile.")
            return

        svc = _unified_tasks()
        if not svc:
            await _send_ephemeral(interaction, "❌ Task service unavailable.")
            return
        from services.unified_task_service import TaskValidationError
        task = await svc.task_store.get_task(task_id)
        if not task:
            await _send_ephemeral(interaction, "❌ Task not found.")
            return
        if task.get("assigned_to") != actor_person_id and get_person_group(interaction.user) != "admin":
            await _send_ephemeral(interaction, "❌ Only the assignee can complete this task.")
            return

        try:
            updated = await svc.complete_task(
                task_id,
                actor_id=actor_person_id,
                note=(note or "").strip(),
                via="slash",
            )
        except TaskValidationError as exc:
            await _send_ephemeral(interaction, f"❌ {exc}")
            return

        await _broadcast_task_update("completed", task_id)

        if updated and bool(updated.get("requires_approval")) and not updated.get("approved_at"):
            await _send_ephemeral(interaction, "✅ Marked done. Waiting for parent approval.")
            return

        await _send_ephemeral(interaction, "✅ Task completed.")


    @tree.command(name="task_snooze", description="Snooze a task reminder")
    @app_commands.describe(task_id="Task ID", preset="How long to snooze")
    @app_commands.choices(preset=[
        app_commands.Choice(name="15 minutes", value="15m"),
        app_commands.Choice(name="1 hour", value="1h"),
        app_commands.Choice(name="Tomorrow 8am", value="tomorrow"),
    ])
    async def cmd_task_snooze(interaction: discord.Interaction, task_id: int, preset: str):
        await interaction.response.defer(ephemeral=True)
        actor_person_id = _discord_to_person_id(interaction.user.id)
        if not actor_person_id:
            await _send_ephemeral(interaction, "❌ I couldn't map your Discord account to a family profile.")
            return

        svc = _unified_tasks()
        if not svc:
            await _send_ephemeral(interaction, "❌ Task service unavailable.")
            return
        from services.unified_task_service import TaskValidationError
        task = await svc.task_store.get_task(task_id)
        if not task:
            await _send_ephemeral(interaction, "❌ Task not found.")
            return
        if task.get("assigned_to") != actor_person_id and get_person_group(interaction.user) != "admin":
            await _send_ephemeral(interaction, "❌ Only the assignee can snooze this task.")
            return

        snooze_until = _snooze_target(preset)
        try:
            await svc.snooze_task(
                task_id,
                actor_id=actor_person_id,
                snooze_until=snooze_until.isoformat(),
                preset=preset,
            )
        except TaskValidationError as exc:
            await _send_ephemeral(interaction, f"❌ {exc}")
            return

        await _broadcast_task_update("snoozed", task_id)
        await _send_ephemeral(interaction, f"💤 Snoozed task #{task_id} until {snooze_until.strftime('%a %b %-d %-I:%M %p')}")


    @tree.command(name="task_no", description="Mark a task as not completed and notify the assigner")
    @app_commands.describe(task_id="Task ID", note="Optional reason")
    async def cmd_task_no(interaction: discord.Interaction, task_id: int, note: str | None = None):
        await interaction.response.defer(ephemeral=True)
        actor_person_id = _discord_to_person_id(interaction.user.id)
        if not actor_person_id:
            await _send_ephemeral(interaction, "❌ I couldn't map your Discord account to a family profile.")
            return

        task = await get_database().get_task(task_id)
        if not task:
            await _send_ephemeral(interaction, "❌ Task not found.")
            return
        if task.get("assigned_to") != actor_person_id and get_person_group(interaction.user) != "admin":
            await _send_ephemeral(interaction, "❌ Only the assignee can mark this task as not done.")
            return

        from services.unified_task_service import TaskValidationError

        svc = _unified_tasks()
        if not svc:
            await _send_ephemeral(interaction, "❌ Task service unavailable.")
            return
        try:
            await svc.report_task_not_completed(
                task_id, actor_id=actor_person_id, note=(note or ""),
            )
        except TaskValidationError as exc:
            await _send_ephemeral(interaction, f"❌ {exc}")
            return

        await _broadcast_task_update("not_completed", task_id)
        await _send_ephemeral(interaction, "Noted. I notified the assigner.")


    @tree.command(name="task_approve", description="Approve or reopen a completed task")
    @app_commands.describe(task_id="Task ID", decision="Approve or reopen")
    @app_commands.choices(decision=[
        app_commands.Choice(name="approve", value="approve"),
        app_commands.Choice(name="reopen", value="reopen"),
    ])
    async def cmd_task_approve(interaction: discord.Interaction, task_id: int, decision: str):
        await interaction.response.defer(ephemeral=True)
        actor_person_id = _discord_to_person_id(interaction.user.id)
        if not actor_person_id:
            await _send_ephemeral(interaction, "❌ I couldn't map your Discord account to a family profile.")
            return

        svc = _unified_tasks()
        if not svc:
            await _send_ephemeral(interaction, "❌ Task service unavailable.")
            return
        from services.unified_task_service import TaskValidationError
        task = await svc.task_store.get_task(task_id)
        if not task:
            await _send_ephemeral(interaction, "❌ Task not found.")
            return

        approver_person_id = task.get("approver_person_id") or task.get("assigned_by")
        if approver_person_id != actor_person_id and get_person_group(interaction.user) != "admin":
            await _send_ephemeral(interaction, "❌ Only the assigner can approve this task.")
            return

        approved = decision == "approve"
        try:
            await svc.approve_task(task_id, actor_id=actor_person_id, approved=approved)
        except TaskValidationError as exc:
            await _send_ephemeral(interaction, f"❌ {exc}")
            return

        await _broadcast_task_update("approved" if approved else "reopened", task_id)
        await _send_ephemeral(interaction, "Approved." if approved else "Reopened.")


    @tree.command(name="automation_add", description="Create a recurring or one-off reminder automation")
    @app_commands.describe(
        title="Automation title",
        message="Reminder message",
        schedule_kind="cron, daily, weekly, hourly, or once",
        schedule="Schedule payload text (see examples below)",
        audience="remind me or remind everyone (#smithy)",
    )
    @app_commands.choices(schedule_kind=[
        app_commands.Choice(name="cron", value="cron"),
        app_commands.Choice(name="daily", value="daily"),
        app_commands.Choice(name="weekly", value="weekly"),
        app_commands.Choice(name="hourly", value="hourly"),
        app_commands.Choice(name="once", value="once"),
    ], audience=[
        app_commands.Choice(name="remind me", value="self"),
        app_commands.Choice(name="remind everyone", value="everyone"),
    ])
    async def cmd_automation_add(
        interaction: discord.Interaction,
        title: str,
        message: str,
        schedule_kind: str,
        schedule: str,
        audience: str = "self",
    ):
        await interaction.response.defer(ephemeral=True)
        actor_person_id = _discord_to_person_id(interaction.user.id)
        if not actor_person_id:
            await _send_ephemeral(interaction, "❌ I couldn't map your Discord account to a family profile.")
            return

        payload: dict
        try:
            if schedule_kind == "cron":
                payload = {"expr": schedule.strip()}
            elif schedule_kind == "daily":
                hh, mm = map(int, schedule.strip().split(":"))
                payload = {"time": f"{hh:02d}:{mm:02d}"}
            elif schedule_kind == "weekly":
                # ex: thu 18:30
                day_txt, hhmm = schedule.strip().split(maxsplit=1)
                hh, mm = map(int, hhmm.split(":"))
                payload = {"day_of_week": weekday_num(day_txt), "time": f"{hh:02d}:{mm:02d}"}
            elif schedule_kind == "hourly":
                minute = int(schedule.strip())
                payload = {"minute": minute}
            elif schedule_kind == "once":
                payload = {"run_at": _parse_datetime_local(schedule).isoformat()}
            else:
                raise ValueError("Unsupported schedule kind")

            next_run = next_automation_run(schedule_kind, payload, config.get("timezone", "America/Halifax"))
        except Exception:
            await _send_ephemeral(interaction,
                "❌ Invalid schedule format."
                "\n`cron`: */30 * * * *"
                "\n`daily`: 18:30"
                "\n`weekly`: thu 18:30"
                "\n`hourly`: 15"
                "\n`once`: 2026-05-09 18:30",
            )
            return

        created = await db_writes.routed("create_automation", 
            title=title.strip(),
            message=message.strip(),
            person_id=actor_person_id,
            schedule_kind=schedule_kind,
            schedule_payload=payload,
            timezone=config.get("timezone", "America/Halifax"),
            created_by=actor_person_id,
            audience_scope=audience,
            next_run_at=next_run.isoformat() if next_run else None,
        )

        when = next_run.strftime("%a %b %-d %-I:%M %p") if next_run else "(no next run)"
        where = "#smithy" if audience == "everyone" else "DM"
        await _send_ephemeral(interaction, f"✅ Automation #{created['id']} created. Next run: {when}. Delivery: {where}.")


    @tree.command(name="automation_list", description="List your automations")
    @app_commands.describe(active_only="Show only active automations")
    async def cmd_automation_list(interaction: discord.Interaction, active_only: bool = True):
        await interaction.response.defer(ephemeral=True)
        actor_person_id = _discord_to_person_id(interaction.user.id)
        if not actor_person_id:
            await _send_ephemeral(interaction, "❌ I couldn't map your Discord account to a family profile.")
            return

        if get_person_group(interaction.user) == "admin":
            rows = await get_database().list_all_automations()
        else:
            rows = await get_database().list_automations_for_person(actor_person_id, include_created_by=True)

        if active_only:
            rows = [r for r in rows if r.get("is_active")]

        if not rows:
            await _send_ephemeral(interaction, "No automations found.")
            return

        lines = []
        for a in rows[:25]:
            nxt = a.get("next_run_at") or "—"
            kind = a.get("schedule_kind", "?")
            scope = a.get("audience_scope", "self")
            status = "on" if a.get("is_active") else "off"
            lines.append(f"#{a['id']} [{status}] {a['title']} ({kind}/{scope}) next: {nxt}")
        await _send_ephemeral(interaction, "\n".join(lines))


    @tree.command(name="automation_toggle", description="Enable or disable an automation")
    @app_commands.describe(automation_id="Automation ID", setting="on or off")
    @app_commands.choices(setting=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    async def cmd_automation_toggle(interaction: discord.Interaction, automation_id: int, setting: str):
        await interaction.response.defer(ephemeral=True)
        actor_person_id = _discord_to_person_id(interaction.user.id)
        if not actor_person_id:
            await _send_ephemeral(interaction, "❌ I couldn't map your Discord account to a family profile.")
            return

        row = await get_database().get_automation(automation_id)
        if not row:
            await _send_ephemeral(interaction, "❌ Automation not found.")
            return
        if get_person_group(interaction.user) != "admin" and actor_person_id not in {row.get("person_id"), row.get("created_by")}:
            await _send_ephemeral(interaction, "❌ You can only manage your own automations.")
            return

        enabled = setting == "on"
        updated = await db_writes.routed("set_automation_active", automation_id, enabled)
        if enabled and updated and not updated.get("next_run_at"):
            nxt = next_automation_run(
                updated.get("schedule_kind", "weekly"),
                updated.get("schedule_payload", {}),
                updated.get("timezone") or config.get("timezone", "America/Halifax"),
            )
            await db_writes.routed("set_automation_next_run", automation_id, nxt.isoformat() if nxt else None)

        await _send_ephemeral(interaction, f"✅ Automation #{automation_id} {'enabled' if enabled else 'disabled'}.")


    @tree.command(name="automation_delete", description="Delete an automation")
    @app_commands.describe(automation_id="Automation ID")
    async def cmd_automation_delete(interaction: discord.Interaction, automation_id: int):
        await interaction.response.defer(ephemeral=True)
        actor_person_id = _discord_to_person_id(interaction.user.id)
        if not actor_person_id:
            await _send_ephemeral(interaction, "❌ I couldn't map your Discord account to a family profile.")
            return

        row = await get_database().get_automation(automation_id)
        if not row:
            await _send_ephemeral(interaction, "❌ Automation not found.")
            return
        if get_person_group(interaction.user) != "admin" and actor_person_id not in {row.get("person_id"), row.get("created_by")}:
            await _send_ephemeral(interaction, "❌ You can only delete your own automations.")
            return

        await db_writes.routed("delete_automation", automation_id)
        await _send_ephemeral(interaction, f"🗑️ Deleted automation #{automation_id}.")

