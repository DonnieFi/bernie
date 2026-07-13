"""API routes: config (family-bot-8lx.2 hard-cut)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, Header
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Optional
import asyncio
import logging
import os
import pathlib
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

from personality_files import (  # noqa: E402
    is_editable_personality_rel,
    list_editable_personality_files,
    normalize_docs_rel,
)


def _personality_file_path(path: str) -> pathlib.Path:
    """Resolve an allowlisted docs path under DOCS_ROOT; raise 403 otherwise."""
    cleaned = normalize_docs_rel(path)
    if not is_editable_personality_rel(cleaned):
        raise HTTPException(
            status_code=403,
            detail="Only personality docs (soul/bernie/family/person notes) are editable here",
        )
    safe_root = pathlib.Path(_ac.DOCS_ROOT).resolve()
    target = (safe_root / cleaned).resolve()
    try:
        target.relative_to(safe_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied") from None
    if not str(target).startswith(str(safe_root) + os.sep) and target != safe_root:
        raise HTTPException(status_code=403, detail="Access denied")
    return target


def build_config_router(ctx: Any) -> APIRouter:
    """Register _ac.config routes; closes over container services via ctx."""
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

    @router.get("/api/config/files", dependencies=[Depends(_ac.verify_token)])
    async def list_config_files():
        root = pathlib.Path(_ac.DOCS_ROOT)
        return list_editable_personality_files(root)

    @router.get("/api/config/files/{path:path}", dependencies=[Depends(_ac.verify_token)])
    async def get_config_file(path: str):
        target = _personality_file_path(path)
        if not target.exists():
            raise HTTPException(status_code=404, detail="File not found")
        return {"path": normalize_docs_rel(path), "content": target.read_text(encoding="utf-8")}

    @router.put("/api/config/files/{path:path}", dependencies=[Depends(_ac.verify_token)])
    async def write_config_file(path: str, data: Dict[str, str]):
        target = _personality_file_path(path)
        if not target.exists():
            raise HTTPException(status_code=404, detail="File not found")
        target.write_text(data.get("content", ""), encoding="utf-8")
        # Behaviour pack is cached in-process; refresh after human edit via UI.
        try:
            from context import invalidate_behaviour_cache
            invalidate_behaviour_cache()
        except Exception:
            pass
        return {"ok": True}

    @router.get("/api/settings", dependencies=[Depends(_ac.verify_token)])
    async def get_settings():
        ha_entities = _ac.config.get("home_assistant", {}).get("entities", [])
        mapped_entities = [{"entity": e["entity_id"], "type": e.get("type", "Light").capitalize(), "area": e.get("room", "Unknown").replace("_", " ").title()} for e in ha_entities]
        members = []
        for person in person_registry.family():
            person_id = person["id"]
            prefs = await db.get_person_pref(person_id=person_id) if person_id else {"reminders_enabled": True, "reminder_minutes": 30}
            macs = person.get("device_macs", [])
            members.append({
                "name": person["display"], 
                "mac": macs[0] if macs else "", 
                "reminders_enabled": prefs.get("reminders_enabled"), 
                "reminder_minutes": prefs.get("reminder_minutes")
            })

        current_model, _ = _ac.get_model_info()
        email_active = os.path.exists(_ac.GMAIL_TOKEN)
        return {
            "members": members, "haEntities": mapped_entities, "summarySchedule": f"{_ac.config.get('summary_hour', 7):02d}:{_ac.config.get('summary_minute', 0):02d} · #smithy",
            "summaryHour": _ac.config.get("summary_hour", 7), "summaryMinute": _ac.config.get("summary_minute", 0),
            "channels": [
                { "name": "Discord", "ico": "DC", "meta": f"Active · {current_model}", "state": "active", "label": "Active" },
                { "name": "SMS",     "ico": "SM", "meta": "Not configured", "state": "off", "label": "Off" },
                { "name": "Email",   "ico": "EM", "meta": "Connected · Gmail API" if email_active else "Not configured", "state": "active" if email_active else "off", "label": "Connected" if email_active else "Off" }
            ]
        }

    @router.put("/api/settings", dependencies=[Depends(_ac.verify_token)])
    async def put_settings(data: Dict[str, Any]):
        from config import update_config
        schedule = data.get("schedule", {})
        updates = {}
        if "summary_hour" in schedule:
            updates["summary_hour"] = int(schedule["summary_hour"])
        if "summary_minute" in schedule:
            updates["summary_minute"] = int(schedule["summary_minute"])
        if updates:
            await update_config(updates)
        return {"ok": True}

    @router.post("/api/config/reload", dependencies=[Depends(_ac.verify_token)])
    async def reload_bot_config():
        try:
            from config import reload_config
            from config_validate import validate_config

            cfg = reload_config()
            findings = validate_config(cfg)
            for f in findings:
                if f.get("severity") == "error":
                    log.warning("config_doctor [%s] %s", f.get("code"), f.get("message"))
            return {"ok": True, "findings": findings}
        except Exception as e:
            log.error(f"Reload error: {e}"); raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/bot/restart", dependencies=[Depends(_ac.require_admin)])
    async def restart_bot(user: _ac.Person = Depends(_ac.require_admin)):
        # family-bot-mu2.5: admin-only process exit
        log.info("Bot restart requested via API by %s", user.id)
        async def delayed_restart():
            await asyncio.sleep(1); os._exit(0)
        asyncio.create_task(delayed_restart())
        return {"ok": True}


    return router
