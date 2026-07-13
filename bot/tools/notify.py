"""Notification tool handlers."""
from __future__ import annotations

from tools import ROLE_ALL, ROLE_PARENTS, tool


@tool(
    name="notify_family_member",
    description=(
        "Send a direct Discord DM to a family member. Messages sent during quiet hours "
        "(10pm–7am) are queued and delivered in the morning unless urgency='high', which "
        "always delivers immediately."
    ),
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "recipient": {"type": "string", "description": "Discord ID or name/alias ('Dad', 'Mom', 'Child1')"},
            "message":   {"type": "string", "description": "Message to send"},
            "urgency":   {"type": "string", "description": "'low', 'normal', or 'high'"},
        },
        "required": ["recipient", "message"],
    },
    role_required=ROLE_PARENTS,
    tier=2,
)
async def handle_notify_family_member(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have notified {args.get('recipient')}: {args.get('message')!r}]"
    from constants import registry as person_registry

    notification_router = ctx.services.orchestrator
    if not notification_router:
        return "Notification service is not available in this context."

    recipient = args["recipient"]
    message = args["message"]
    urgency = args.get("urgency", "normal")

    person_id = person_registry.resolve(recipient)
    person = person_registry.get(person_id) if person_id else None
    discord_id = person.get("discord_id") if person else None

    if not discord_id and str(recipient).isdigit():
        discord_id = str(recipient)

    if not discord_id:
        return f"Could not find Discord ID for {recipient}."

    await notification_router.notify(notification_router.notification(
        recipient_id=discord_id,
        message=message,
        urgency=urgency,
    ))
    return f"Notification sent to {recipient}."


# ── User preference parity for /reminders /dm /settings ───────────────────────
@tool(
    name="set_reminders",
    description="Toggle personal reminder pings in channel (on or off). Matches /reminders slash. Uses public DB prefs.",
    input_schema={
        "type": "object",
        "properties": {
            "setting": {"type": "string", "description": "on or off"},
        },
        "required": ["setting"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_set_reminders(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would set reminders {args}]"
    import db_writes

    setting = args["setting"].lower()
    enabled = setting in ("on", "true", "1", "yes")
    pid = ctx.person_id or "unknown"
    await db_writes.set_person_pref(pid, reminders_enabled=enabled)
    state = "on" if enabled else "off"
    return f"Reminders set to {state} for you."

@tool(
    name="set_dm_mode",
    description="Toggle personal DM delivery for reminders instead of channel mentions (on or off). Matches /dm slash.",
    input_schema={
        "type": "object",
        "properties": {
            "setting": {"type": "string", "description": "on or off"},
        },
        "required": ["setting"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_set_dm_mode(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would set dm_mode {args}]"
    import db_writes

    setting = args["setting"].lower()
    enabled = setting in ("on", "true", "1", "yes")
    pid = ctx.person_id or "unknown"
    await db_writes.set_person_pref(pid, dm_mode=enabled)
    state = "on" if enabled else "off"
    return f"DM mode set to {state} for you."

@tool(
    name="get_settings",
    description="View your current personal Bernie preferences (reminders, dm_mode, etc). Matches /settings slash.",
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_settings(args: dict, ctx) -> str:
    if ctx.shadow:
        return "[shadow: would return your settings]"
    from db_binding import get_database

    db = ctx.services.db if ctx.services and ctx.services.db else get_database()
    pid = ctx.person_id or "unknown"
    prefs = await db.get_person_pref(person_id=pid)
    rem = "on" if prefs.get("reminders_enabled", True) else "off"
    dm = "on" if prefs.get("dm_mode", True) else "off"
    lead = prefs.get("reminder_minutes", 30)
    return f"Your settings: reminders={rem}, dm_mode={dm}, reminder_minutes={lead}"
