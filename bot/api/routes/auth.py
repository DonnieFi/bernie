"""API routes: auth (family-bot-8lx.2 hard-cut)."""
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


def build_auth_router(ctx: Any) -> APIRouter:
    """Register auth routes; closes over container services via ctx."""
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


    @router.get("/")
    async def root(): return FileResponse(f"{_ac.WEB_ROOT}/index.html")

    @router.get("/api/auth/users")
    async def get_auth_users():
        family_members = _ac.config.get("family_members", {})
        users = []
        for person in person_registry.family():
            display_name = person.get("display")
            config_info = family_members.get(display_name, {})
            users.append({
                "name": display_name,
                "color": config_info.get("color", "var(--ink-2)")
            })
        return users

    login_attempts = {}
    trusted_proxy_cidrs = _ac.config.get("trusted_proxy_cidrs", [
        "127.0.0.1/32",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ])
    trusted_proxy_networks = []
    for cidr in trusted_proxy_cidrs:
        try:
            trusted_proxy_networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            log.warning(f"Ignoring invalid trusted proxy CIDR: {cidr}")

    def _is_trusted_proxy_ip(peer_ip: str) -> bool:
        try:
            peer = ipaddress.ip_address(peer_ip)
        except ValueError:
            return False
        return any(peer in network for network in trusted_proxy_networks)

    def _client_ip_for_rate_limit(request: Request) -> str:
        peer_ip = request.client.host if request.client else "unknown"
        forwarded = request.headers.get("X-Forwarded-For")
        if not forwarded:
            return peer_ip

        if not _is_trusted_proxy_ip(peer_ip):
            return peer_ip

        forwarded_ip = forwarded.split(",")[0].strip()
        if not forwarded_ip:
            return peer_ip
        try:
            ipaddress.ip_address(forwarded_ip)
            return forwarded_ip
        except ValueError:
            return peer_ip

    @router.post("/api/auth/login")
    async def auth_login(data: Dict[str, str], request: Request):
        from starlette.concurrency import run_in_threadpool
        
        client_ip = _client_ip_for_rate_limit(request)
            
        now = time.time()
        
        # Clean up old attempts
        for ip in list(login_attempts.keys()):
            if now - login_attempts[ip]["last_attempt"] > 60:
                del login_attempts[ip]
                
        # Check rate limit
        attempt_info = login_attempts.get(client_ip, {"count": 0, "last_attempt": 0})
        if attempt_info["count"] >= 5 and (now - attempt_info["last_attempt"]) < 60:
            raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
            
        attempt_info["count"] += 1
        attempt_info["last_attempt"] = now
        login_attempts[client_ip] = attempt_info

        person_name_input = data.get("person")
        pin = data.get("pin")
        
        if not person_name_input or not pin:
            raise HTTPException(status_code=400, detail="Missing person or pin")
        
        person_id = person_registry.resolve(person_name_input)
        if not person_id:
            raise HTTPException(status_code=404, detail="_ac.Person not found")

        person = person_registry.get(person_id)
        display_name = person.get("display", person_id)

        if person.get("role") == "friend":
            raise HTTPException(status_code=403, detail="Web dashboard access is not available for this account")

        # PIN hash is not in registry, must get from original _ac.config
        family_members = _ac.config.get("family_members", {})
        person_config = family_members.get(display_name, {})
            
        stored_hash = person_config.get("web_pin_hash")
        if not stored_hash:
            raise HTTPException(status_code=403, detail="No PIN configured for this person")
            
        import auth_service
        is_valid = await run_in_threadpool(auth_service.verify_pin, pin, stored_hash)
        if not is_valid:
            raise HTTPException(status_code=401, detail="Incorrect password / PIN")
            
        # On success, clear attempts
        if client_ip in login_attempts:
            del login_attempts[client_ip]
            
        secret = _ac.config.get("bernie_api_token") or os.environ.get("BERNIE_API_TOKEN")
        role = person.get("role", "family")
        
        jwt_token = auth_service.create_jwt({"person_id": person_id, "role": role}, secret)
        
        return {"ok": True, "token": jwt_token, "person": {"id": person_id, "role": role, "name": display_name}}

    @router.get("/api/me", dependencies=[Depends(_ac.verify_token)])
    async def get_me(user: _ac.Person = Depends(_ac.verify_token)):
        real_name = person_registry.display_name(user.id)
        prefs = await db.get_person_pref(person_id=user.id)
        presence = await presence_service.get_full_presence()
        
        return {
            "id": user.id,
            "name": real_name,
            "role": user.role,
            "family_name": _ac.config.get("family_name", "Example"),
            "preferences": prefs,
            "presence": presence.get(user.id, {}),
            "openwebui_url": _ac.config.get("openwebui_url", "https://ai.lan/")
        }


    return router
