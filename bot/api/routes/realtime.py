"""API routes: realtime (family-bot-8lx.2 hard-cut)."""
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


def build_realtime_router(ctx: Any) -> APIRouter:
    """Register realtime routes; closes over container services via ctx."""
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

    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        expected = _ac.config.get("bernie_api_token") or os.environ.get("BERNIE_API_TOKEN")
        token = websocket.query_params.get("token")
        
        if not token:
            await websocket.accept()
            try:
                auth = await asyncio.wait_for(websocket.receive_json(), timeout=5)
                token = auth.get("token")
            except (WebSocketDisconnect, asyncio.TimeoutError):
                return
            except Exception as e:
                log.warning(f"WebSocket auth handshake failed: {e}")
                await websocket.close(code=1008); return
        else:
            await websocket.accept()

        payload = auth_service.verify_jwt(token, expected) if expected and token else None
        is_valid_jwt = payload is not None
        is_valid_master = secrets.compare_digest(token, expected) if expected and token else False
        
        if not expected or (not is_valid_master and not is_valid_jwt):
            await websocket.close(code=1008); return

        connection_manager.active_connections.append(websocket)
        try:
            while True: await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.warning(f"WebSocket error in /ws: {e}")
        finally:
            connection_manager.disconnect(websocket)

    @router.get("/api/logs", dependencies=[Depends(_ac.verify_token)])
    async def get_logs(n: int = 200, user: _ac.Person = Depends(_ac.verify_token)):
        """One-shot bot.log snapshot (no live WS tail — review: drop poller)."""
        if user.role not in ("admin", "parents"):
            raise HTTPException(status_code=403, detail="admin/parents only")
        limit = max(1, min(int(n or 200), 1000))

        def _read_tail(path: str, count: int) -> list[str]:
            from collections import deque
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    return [ln.rstrip() for ln in deque(f, maxlen=count) if ln.strip()]
            except FileNotFoundError:
                return []

        lines = await asyncio.to_thread(_read_tail, _ac.BOT_LOG, limit)
        return {"lines": lines, "path": "bot.log", "n": len(lines)}



    return router
