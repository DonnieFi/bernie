"""Cognition inbound write service (40A-1).

Allowlisted database write operations exposed over internal HTTP on the
cognition container. Discord and API roles call via db_client.cognition_db_write.

Network: listens on port 9000 inside bernie-cognition — bernie-net only, never
published to the host (same pattern as bernie-discord /internal/post on :9000).

WRITE_OPS is intentionally small at 40A-1; 40A-3 adds discord hot-path ops,
40A-4 adds API writes. Any new op must be registered here and covered by tests.
"""

import logging
import os
from typing import Any, Awaitable, Callable

import database

logger = logging.getLogger(__name__)

WriteHandler = Callable[..., Awaitable[Any]]

# Expanded in 40A-3/4/5b — keep allowlist explicit; TestWriteOpsRegistry guards binding.
_WRITE_OP_NAMES: tuple[str, ...] = (
    # 40A-3 discord hot paths
    "add_message",
    "mark_reminder_sent",
    "set_person_pref",
    "save_rsvp",
    "store_message_mapping",
    # 40A-4 unified_tasks / SQLiteTaskStore
    "create_agent_task",
    "create_task",
    "update_task",
    "delete_task",
    "complete_task",
    "approve_task",
    "snooze_task",
    "block_task",
    "reassign_task",
    "convert_task_type",
    "set_kanban_status",
    "set_unified_task_running",
    "update_unified_task_heartbeat",
    "add_task_event",
    "start_execution",
    "finish_execution",
    "link_tasks",
    "promote_ready_tasks",
    "log_activity",
    # 40A-4 api.py direct
    "create_automation",
    "set_automation_active",
    "delete_automation",
    "create_chat_thread",
    "update_chat_thread_title",
    "delete_chat_thread",
    "add_chat_message",
    "update_presence",
    "apply_presence_tick",
    "delete_memory_event",
    "delete_memory_events_for_person",
    # 40A-5b stragglers (discord/api BTS, listeners, tools, instrumentation)
    "set_last_home_signal",
    "save_ha_devices",
    "save_network_devices_store",
    "identity_log_unresolved",
    "prune_email_send_rate",
    "insert_email_signal",
    "set_email_ingest_history_id",
    "ensure_email_schema",
    "update_email_pending_smithy_message",
    "record_email_send",
    "create_email_pending",
    "resolve_email_pending",
    "claim_email_pending_for_send",
    "finalize_email_pending",
    "expire_stale_email_pending",
    "mark_task_prompted",
    "mark_task_escalated",
    "mark_automation_triggered",
    "set_automation_next_run",
    "create_cognitive_task",
    "update_cognitive_task_payload",
    "mark_draft_posted",
    "delete_draft",
    "store_draft",
    "add_pending_notification",
    "clear_pending_notifications_by_ids",
    "log_notification",
    "upsert_host_ip_snapshot",
    "record_network_event",
    "ensure_network_watchman_schema",
    "insert_memory_event",
    "prune_memory_events_before",
    "save_weather_location",
    "set_weather_snapshot",
    "set_meal",
    "delete_meal",
    "add_grocery",
    "remove_grocery",
    "create_pending_hitl",
    "resolve_pending_hitl",
    "set_pending_hitl_notify_message_ids",
    "expire_stale_pending_hitl",
    "purge_terminal_pending_hitl",
    "log_token_usage",
    "log_llm_iteration",
    "log_context_build",
    "log_tool_surface",
    "log_turn_timing",
    "cache_session_title",
    "decay_routines",
    "set_db_metadata",
    "prune_stale_low_confidence_routines",
    "prune_stale_low_confidence_observations",
)

WRITE_OPS: dict[str, WriteHandler] = {
    name: getattr(database, name) for name in _WRITE_OP_NAMES
}


def verify_internal_auth(x_internal_auth: str | None) -> None:
    """Fail-closed auth check; raises HTTPException on failure."""
    from fastapi import HTTPException

    secret = os.environ.get("INTERNAL_POST_SECRET")
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="internal writes disabled: INTERNAL_POST_SECRET not set",
        )
    if x_internal_auth != secret:
        raise HTTPException(status_code=403, detail="Forbidden")


async def execute_write_op(op: str, kwargs: dict[str, Any]) -> Any:
    handler = WRITE_OPS.get(op)
    if handler is None:
        raise ValueError(f"unknown write op: {op!r}")
    return await handler(**kwargs)


def create_internal_write_app():
    """FastAPI app for POST /internal/db/write (cognition role, port 9000)."""
    from fastapi import FastAPI, HTTPException, Header
    from pydantic import BaseModel

    internal_app = FastAPI(title="Bernie Cognition Internal Writes (40A)")

    @internal_app.get("/internal/db/health")
    async def internal_db_health():
        return {"ok": True, "role": "cognition"}

    class InternalDbWritePayload(BaseModel):
        op: str
        kwargs: dict[str, Any] = {}

    @internal_app.post("/internal/db/write")
    async def internal_db_write(
        payload: InternalDbWritePayload,
        x_internal_auth: str | None = Header(None, alias="X-Internal-Auth"),
    ):
        # FastAPI infers JSON body from the sole BaseModel param; explicit Body() is only
        # needed on internal_discord routes (see internal_discord.py — FastAPI 0.139 quirk).
        verify_internal_auth(x_internal_auth)
        if payload.op not in WRITE_OPS:
            raise HTTPException(status_code=400, detail=f"unknown write op: {payload.op!r}")
        try:
            result = await execute_write_op(payload.op, payload.kwargs)
            return {"success": True, "result": result}
        except TypeError as e:
            raise HTTPException(status_code=400, detail=f"invalid kwargs for {payload.op!r}: {e}") from e
        except Exception as e:
            logger.error("internal_db_write failed op=%s: %s", payload.op, e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    return internal_app


async def run_internal_write_server() -> None:
    """Listen on 9000 (bernie-net only — not published to host)."""
    import uvicorn

    internal_app = create_internal_write_app()
    logger.info("cognition internal write server listening on 0.0.0.0:9000")
    cfg = uvicorn.Config(internal_app, host="0.0.0.0", port=9000, log_level="warning")
    server = uvicorn.Server(cfg)
    await server.serve()
