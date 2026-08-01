"""Shared module-level API helpers, models, and auth deps (family-bot-8lx.2)."""
from fastapi import FastAPI, Security, HTTPException, Depends, WebSocket, WebSocketDisconnect, Query, Header, Request
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import logging
import time
import json
import ipaddress
from datetime import datetime, timedelta, timezone
import asyncio
import os
import pathlib
import aiohttp
import auth_service
import secrets
from zoneinfo import ZoneInfo
from starlette.websockets import WebSocketDisconnect

# Internal imports
from config import config, ROOT_DIR, DOCS_ROOT, WEB_ROOT
from presence_service import presence_service
from ha_service import ha_service
from weather_service import get_weather
from frigate_service import frigate_service
from garbage_service import get_next_collections, get_tomorrow_collection
from constants import registry as person_registry, PERSON_IDS, PERSON_DISPLAY, HA_SUPPORT_BRIGHTNESS, HA_SUPPORT_COLOR_TEMP, HA_SUPPORT_RGB_COLOR, HA_RGB_MODES, HA_DIM_MODES
from utils.discord_helpers import next_automation_run
from recommendation_engine import get_recommendations
import summary_builder
from llm.chat import chat_general
from llm.model_state import get_model_info

# Setup logging
log = logging.getLogger(__name__)

# Constants
BOT_LOG = f"{ROOT_DIR}/data/bot.log" if ROOT_DIR == "/opt/family-bot" else "/data/bot.log"
CONFIG_FILE = f"{ROOT_DIR}/config.json"
GMAIL_TOKEN = f"{ROOT_DIR}/credentials/gmail_token.json" if ROOT_DIR == "/opt/family-bot" else "/credentials/gmail_token.json"

BOT_START_TIME = datetime.now()

# Caching
_note_cache: Dict[str, str] = {}
_note_refresh_ts: Dict[str, float] = {}   # last *successful* refresh time per day-key
_note_inflight: set = set()               # keys with an in-flight background refresh
_NOTE_REFRESH_INTERVAL = 3600             # re-generate note at most once per hour
_cache: Dict[str, tuple] = {}
_TTL_HIGHLIGHTS = 300   # 5 min

def _cache_get(key: str, ttl: float) -> Any:
    entry = _cache.get(key)
    if entry and (time.monotonic() - entry[0]) < ttl:
        return entry[1]
    return None

def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)

def _format_uptime(delta_seconds: float) -> str:
    s = int(delta_seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d: return f"{d}d {h}h"
    if h: return f"{h}h {m}m"
    return f"{m}m"

async def _build_formatted_highlights(session, config, calendar_service, summary_builder_mod, tz, w=None, events=None) -> list:
    if w is None:
        from weather_service import get_weather
        w = await get_weather(config.get("location", {}).get("lat", 44.6476), config.get("location", {}).get("lon", -63.5728), session)
    rec = get_recommendations(w) if w else None
    if events is None:
        try:
            events = await calendar_service.get_todays_events()
        except Exception as e:
            log.warning(f"Calendar fetch skipped in highlights: {e}")
            events = []
    from school_calendar import exclude_school_from_schedule, school_calendar_ids, show_school_in_daily_summary

    events = exclude_school_from_schedule(events, config)

    summary_school_cals = (
        school_calendar_ids(config) if show_school_in_daily_summary(config) else set()
    )
    ics_url = config.get("recollect_ics_url")
    gtc = await get_tomorrow_collection(ics_url, tz, session) if ics_url else None
    raw = summary_builder_mod.build_highlights(
        events, rec, gtc is not None, tz, school_cals=summary_school_cals,
    )
    raw.sort(key=lambda h: h.urgency, reverse=True)
    return [{"kind": h.kind, "title": h.text, "meta": "", "right": h.emoji} for h in raw[:3]]

API_TOKEN_NAME = "X-Bernie-Token"
api_key_header = APIKeyHeader(name=API_TOKEN_NAME, auto_error=False)

class Person(BaseModel):
    id: str
    role: str

def _master_token_ok(token: str | None, expected: str | None) -> bool:
    """Constant-time master token compare (family-bot-mu2.2)."""
    if not token or not expected:
        return False
    try:
        return secrets.compare_digest(token, expected)
    except (TypeError, ValueError):
        return False


async def verify_token(api_key_header: str | None = Security(api_key_header)) -> Person:
    expected = config.get("bernie_api_token") or os.environ.get("BERNIE_API_TOKEN")
    if not expected: raise HTTPException(status_code=500, detail="API Token not configured")
    if not api_key_header: raise HTTPException(status_code=401, detail="Not authenticated")
    
    import auth_service
    payload = auth_service.verify_jwt(api_key_header, expected)
    if payload:
        return Person(id=payload.get("person_id", "unknown"), role=payload.get("role", "family"))
        
    # Master token → service principal (not a family person). Prefer JWT person_id
    # for prefs/scoped CRUD; paths that use user.id as a person may no-op or audit
    # as api_service when the master token is used.
    if _master_token_ok(api_key_header, expected):
        return Person(id="api_service", role="admin")
        
    raise HTTPException(status_code=403, detail="Invalid API Token")

async def verify_bearer_token(authorization: Optional[str] = Header(None)) -> Person:
    expected = config.get("bernie_api_token") or os.environ.get("BERNIE_API_TOKEN")
    if not expected: raise HTTPException(status_code=500, detail="API token not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization[7:]
    
    import auth_service
    payload = auth_service.verify_jwt(token, expected)
    if payload:
        return Person(id=payload.get("person_id", "unknown"), role=payload.get("role", "family"))
        
    if _master_token_ok(token, expected):
        return Person(id="api_service", role="admin")
        
    raise HTTPException(status_code=403, detail="Invalid API token")


async def require_admin(user: Person = Depends(verify_token)) -> Person:
    """family-bot-mu2.5: gate restart / dangerous config writes to admin."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)
    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections: self.active_connections.remove(ws)
    async def broadcast(self, data: dict):
        for connection in list(self.active_connections):
            try: await connection.send_json(data)
            except Exception: self.disconnect(connection)

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    history: Optional[List[ChatMessage]] = []
    person: Optional[str] = None
    thread_id: Optional[str] = None

class ThreadTitleRequest(BaseModel):
    title: str

class LightControl(BaseModel):
    on: bool
    brightness: Optional[int] = None
    color_temp: Optional[int] = None
    rgb: Optional[List[int]] = None

class SwitchControl(BaseModel):
    on: Optional[bool] = None

class MediaCommand(BaseModel):
    command: str
    volume: Optional[float] = None

class PingRequest(BaseModel):
    text: str = "Bernie is pinging you!"

