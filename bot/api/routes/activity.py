"""API routes: activity (family-bot-8lx.2 hard-cut)."""
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


def build_activity_router(ctx: Any) -> APIRouter:
    """Register activity routes; closes over container services via ctx."""
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

    @router.get("/api/activity", dependencies=[Depends(_ac.verify_token)])
    async def get_activity(period: str = Query("30d", pattern="^(7d|30d|90d)$")):
        from activity_aggregator import get_activity_dashboard
        return await get_activity_dashboard(period=period, force_refresh=False)
        
    @router.post("/api/activity/refresh", dependencies=[Depends(_ac.verify_token)])
    async def refresh_activity(period: str = Query("30d", pattern="^(7d|30d|90d)$")):
        from activity_aggregator import get_activity_dashboard
        return await get_activity_dashboard(period=period, force_refresh=True)

    @router.get("/api/usage", dependencies=[Depends(_ac.verify_token)])
    async def get_usage(): return await db.get_token_usage_stats(30)

    @router.get("/api/keys/status", dependencies=[Depends(_ac.verify_token)])
    async def get_keys_status():
        from activity_aggregator import get_provider_accounts
        accounts = await get_provider_accounts(use_cache=True)
        
        anth_acc = next((a for a in accounts if a["provider"] == "anthropic"), {})
        or_acc = next((a for a in accounts if a["provider"] == "openrouter"), {})
        
        anth_spend_30d = await db.get_anthropic_spend_since(
            (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        )
        
        or_keys = _ac.config.get("openrouter_keys", [{"env": "OPENROUTER_API_KEY"}])
        has_or_key = any(os.environ.get(k.get("env", "")) for k in or_keys)

        return {
            "anthropic": {
                "configured": True, 
                "model": anth_acc.get("activeModel"), 
                "spend_30d": anth_spend_30d,
                "last_used": anth_acc.get("lastUsedAt"), 
                "topup_amount": anth_acc.get("budget"), 
                "topup_as_of": anth_acc.get("lastToppedUp", {}).get("at") if anth_acc.get("lastToppedUp") else None, 
                "spend_since": max(0, anth_acc.get("budget", 0) - anth_acc.get("balanceRemaining", 0)), 
                "remaining": anth_acc.get("balanceRemaining")
            },
            "openrouter": {
                "configured": has_or_key, 
                "last_used": or_acc.get("lastUsedAt"),
                "limit_remaining": None, 
                "limit": or_acc.get("budget"),
                "usage_monthly": await db.get_or_spend(30), 
                "usage_weekly": await db.get_or_spend(7), 
                "usage_daily": await db.get_or_spend(1),
                "or_balance": None,
                "account_balance": or_acc.get("balanceRemaining")
            }
        }


    return router
