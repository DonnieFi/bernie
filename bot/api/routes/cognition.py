"""API routes: cognition (family-bot-8lx.2 hard-cut)."""
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


def build_cognition_router(ctx: Any) -> APIRouter:
    """Register cognition routes; closes over container services via ctx."""
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

    # ── Phase 26-05: /api/cognition/* endpoints ─────────────────────────────

    def _require_admin_or_parents(user: _ac.Person) -> None:
        if user.role not in ("admin", "parents"):
            raise HTTPException(status_code=403, detail="admin/parents only")

    @router.get("/api/cognition/runs", dependencies=[Depends(_ac.verify_token)])
    async def cognition_runs(
        user: _ac.Person = Depends(_ac.verify_token),
        limit: int = 50,
        offset: int = 0,
        type: str | None = None,
    ):
        _require_admin_or_parents(user)
        rows = await db.get_cognitive_runs(limit=min(200, max(1, limit)), offset=max(0, offset), task_type=type)
        return {"runs": rows}

    @router.get("/api/cognition/outputs", dependencies=[Depends(_ac.verify_token)])
    async def cognition_outputs(
        user: _ac.Person = Depends(_ac.verify_token),
        limit: int = 50,
        offset: int = 0,
    ):
        _require_admin_or_parents(user)
        rows = await db.get_cognitive_outputs(limit=min(200, max(1, limit)), offset=max(0, offset))
        return {"outputs": rows}

    @router.get("/api/cognition/stats", dependencies=[Depends(_ac.verify_token)])
    async def cognition_stats(
        user: _ac.Person = Depends(_ac.verify_token),
        days: int = 7,
    ):
        _require_admin_or_parents(user)
        rows = await db.get_cognitive_stats(days=max(1, min(60, days)))
        return {"days": days, "stats": rows}


    return router
