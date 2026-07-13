"""Routed DB writes for discord/api roles (40A-3 / 40A-4).

Reads stay on get_database(); mutations in scope route through cognition when
ROLE is discord or api.
"""

from __future__ import annotations

from typing import Any

from db_client import cognition_db_write


async def add_message(channel_id: int, role: str, content: str) -> None:
    await cognition_db_write("add_message", channel_id=channel_id, role=role, content=content)


async def mark_reminder_sent(event_id: str, remind_min: int) -> None:
    await cognition_db_write("mark_reminder_sent", event_id=event_id, remind_min=remind_min)


async def set_person_pref(person_id: str, discord_id: int | None = None, **kwargs: Any) -> None:
    await cognition_db_write(
        "set_person_pref",
        person_id=person_id,
        discord_id=discord_id,
        **kwargs,
    )


async def save_rsvp(event_id: str, discord_id: int, name: str, status: str) -> None:
    await cognition_db_write(
        "save_rsvp",
        event_id=event_id,
        discord_id=discord_id,
        name=name,
        status=status,
    )


async def store_message_mapping(
    message_id: int,
    event_id: str,
    event_title: str | None = None,
    message_type: str = "event",
) -> None:
    await cognition_db_write(
        "store_message_mapping",
        message_id=message_id,
        event_id=event_id,
        event_title=event_title,
        message_type=message_type,
    )


async def create_automation(
    title: str,
    message: str,
    person_id: str,
    schedule_kind: str,
    schedule_payload: dict,
    timezone: str,
    created_by: str,
    audience_scope: str = "self",
    next_run_at: str | None = None,
) -> dict:
    return await cognition_db_write(
        "create_automation",
        title=title,
        message=message,
        person_id=person_id,
        schedule_kind=schedule_kind,
        schedule_payload=schedule_payload,
        timezone=timezone,
        created_by=created_by,
        audience_scope=audience_scope,
        next_run_at=next_run_at,
    )


async def set_automation_active(automation_id: int, is_active: bool) -> dict | None:
    return await cognition_db_write(
        "set_automation_active", automation_id=automation_id, is_active=is_active
    )


async def delete_automation(automation_id: int) -> None:
    await cognition_db_write("delete_automation", automation_id=automation_id)


async def create_chat_thread(thread_id: str, title: str, person_id: str) -> None:
    await cognition_db_write(
        "create_chat_thread", thread_id=thread_id, title=title, person_id=person_id
    )


async def update_chat_thread_title(thread_id: str, title: str) -> None:
    await cognition_db_write(
        "update_chat_thread_title", thread_id=thread_id, title=title
    )


async def delete_chat_thread(thread_id: str) -> None:
    await cognition_db_write("delete_chat_thread", thread_id=thread_id)


async def add_chat_message(thread_id: str, role: str, content: str) -> None:
    await cognition_db_write(
        "add_chat_message", thread_id=thread_id, role=role, content=content
    )


async def update_presence(
    person_id: str,
    is_home: bool,
    device_mac: str | None = None,
    **kwargs,
) -> None:
    await cognition_db_write(
        "update_presence",
        person_id=person_id,
        is_home=is_home,
        device_mac=device_mac,
        **kwargs,
    )


async def delete_memory_event(person_id: str, event_id: int) -> None:
    await cognition_db_write(
        "delete_memory_event", person_id=person_id, event_id=event_id
    )


async def delete_memory_events_for_person(person_id: str) -> None:
    await cognition_db_write("delete_memory_events_for_person", person_id=person_id)


async def log_activity(
    event_type: str,
    description: str,
    meta: str | None = None,
    channel: str | None = None,
    person_id: str | None = None,
) -> None:
    await cognition_db_write(
        "log_activity",
        event_type=event_type,
        description=description,
        meta=meta,
        channel=channel,
        person_id=person_id,
    )


async def set_last_home_signal(person_id: str, ts: float) -> None:
    await cognition_db_write("set_last_home_signal", person_id=person_id, ts=ts)


async def apply_presence_tick(updates: list[dict]) -> list[tuple[str, bool]]:
    """Batch presence poll writes (family-bot-ah5.4)."""
    return await cognition_db_write("apply_presence_tick", updates=updates)


async def routed(op: str, /, *args: Any, **kwargs: Any) -> Any:
    """Generic allowlisted write — binds positional args to the database handler signature."""
    if args:
        import inspect

        import database

        handler = getattr(database, op, None)
        if handler is None:
            raise ValueError(f"unknown write op: {op!r}")
        params = list(inspect.signature(handler).parameters.keys())
        bound = dict(zip(params, args))
        bound.update(kwargs)
        kwargs = bound
    return await cognition_db_write(op, **kwargs)


async def routed_best_effort(op: str, /, *args: Any, **kwargs: Any) -> Any:
    """Like routed() but logs and returns None on RPC failure (non-critical paths)."""
    if args:
        import inspect

        import database

        handler = getattr(database, op, None)
        if handler is None:
            raise ValueError(f"unknown write op: {op!r}")
        params = list(inspect.signature(handler).parameters.keys())
        bound = dict(zip(params, args))
        bound.update(kwargs)
        kwargs = bound
    return await cognition_db_write(op, required=False, **kwargs)
