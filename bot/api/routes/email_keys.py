"""API routes: email_keys (family-bot-8lx.2 hard-cut)."""
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


def build_email_keys_router(ctx: Any) -> APIRouter:
    """Register email_keys routes; closes over container services via ctx."""
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

    @router.post("/api/email/send", dependencies=[Depends(_ac.verify_token)])
    async def send_email_endpoint(data: Dict[str, str], user: _ac.Person = Depends(_ac.verify_token)):
        if user.role not in ("admin", "parents"):
            raise HTTPException(status_code=403, detail="Parents or admin only")
        to = data.get("to", "").strip()
        subject = data.get("subject", "").strip()
        body = data.get("body", "").strip()
        cc = (data.get("cc") or "").strip()
        cc_list = [p.strip() for p in cc.split(",") if p.strip()] if cc else None
        if not to or not subject or not body:
            raise HTTPException(status_code=400, detail="to, subject, and body are required")
        try:
            from email_service import EmailPolicyError, EmailRateLimitError, send

            msg_id = await send(
                to,
                subject,
                body,
                cc=cc_list,
                requester_id=user.id,
                requester_role=user.role,
                config=_ac.config,
            )
            await db_writes.log_activity(
                event_type="email_api_send",
                description=f"API email to {to}: {subject[:80]}",
                person_id=user.id,
            )
            return {"ok": True, "message_id": msg_id}
        except (EmailPolicyError, EmailRateLimitError) as e:
            raise HTTPException(status_code=403, detail=str(e))
        except Exception as e:
            log.error(f"Email send failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/keys/anthropic/topup", dependencies=[Depends(_ac.verify_token)])
    async def record_anthropic_topup(data: Dict[str, Any]):
        from config import update_config
        amount = float(data.get("amount", 0))
        if amount <= 0:
            raise HTTPException(status_code=400, detail="Amount must be positive")
        as_of = datetime.now(ZoneInfo(_ac.config.get("timezone", "America/Halifax"))).strftime("%Y-%m-%d")
        await update_config({"anthropic_credits": {"amount": amount, "as_of": as_of}})
        log.info(f"Anthropic credits recorded: ${amount:.2f} as of {as_of}")
        return {"ok": True, "amount": amount, "as_of": as_of}

    @router.post("/api/usage/refresh", dependencies=[Depends(_ac.verify_token)])
    async def refresh_usage():
        return {"ok": True}


    return router
