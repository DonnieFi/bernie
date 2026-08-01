"""API routes: health (family-bot-8lx.2 hard-cut)."""
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


def build_health_router(ctx: Any) -> APIRouter:
    """Register health routes; closes over container services via ctx."""
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

    @router.get("/api/health")
    async def health():
        m, _ = _ac.get_model_info()
        from model_registry import model_source
        from model_catalog import provider_readiness_map
        from subscription_complete import fetch_runner_health
        try:
            runner = await fetch_runner_health(_ac.config)
        except Exception:
            runner = {}
        readiness = provider_readiness_map(_ac.config, runner_health=runner)
        try:
            provider = model_source(m, _ac.config)
        except Exception:
            provider = "unknown"
        payload = {
            "status": "ok",
            "model": m,
            "provider": provider,
            "provider_readiness": {
                k: readiness.get(k)
                for k in ("anthropic", "openrouter", "litellm", "ollama", "codex", "grok")
            },
            "uptime": _ac._format_uptime((datetime.now() - _ac.BOT_START_TIME).total_seconds()),
            "bot_connected": bot.is_ready() if bot else False,
        }
        # family-bot-995 polish: queue wait when cognition worker is in-process.
        # Split-role: bernie-api/discord do not run CognitiveWorker — cognitive_queue
        # is absent there by design (export from bernie-cognition health if needed later).
        try:
            import worker as _worker_mod
            cw = getattr(_worker_mod, "_cognitive_worker", None)
            if cw is not None and hasattr(cw, "queue_wait_stats"):
                payload["cognitive_queue"] = cw.queue_wait_stats()
        except Exception:
            pass
        return payload
    @router.get("/api/scheduler")
    async def scheduler_status():
        try:
            from background_scheduler import get_scheduler
            return get_scheduler().get_status()
        except RuntimeError:
            return {"started": False, "tasks": {}, "error": "scheduler not initialized"}


    return router
