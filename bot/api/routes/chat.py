"""API routes: chat (family-bot-8lx.2 hard-cut)."""
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


class OAIMessage(BaseModel):
    role: str
    content: str

class OAICompletionRequest(BaseModel):
    model: str = "bernie"
    messages: List[OAIMessage]
    stream: Optional[bool] = True
    user: Optional[str] = None

def build_chat_router(ctx: Any) -> APIRouter:
    """Register chat routes; closes over container services via ctx."""
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

    @router.get("/api/chat/threads", dependencies=[Depends(_ac.verify_token)])
    async def get_threads(user: _ac.Person = Depends(_ac.verify_token)): 
        return await db.get_chat_threads(user.id)

    @router.post("/api/chat/threads", dependencies=[Depends(_ac.verify_token)])
    async def create_thread(req: _ac.ThreadTitleRequest, user: _ac.Person = Depends(_ac.verify_token)):
        import uuid
        tid = str(uuid.uuid4())
        await db_writes.create_chat_thread(tid, req.title, user.id)
        return {"id": tid, "title": req.title}

    @router.get("/api/chat/threads/{thread_id}/messages", dependencies=[Depends(_ac.verify_token)])
    async def get_thread_messages(thread_id: str): return await db.get_chat_thread_messages(thread_id)

    @router.patch("/api/chat/threads/{thread_id}", dependencies=[Depends(_ac.verify_token)])
    async def update_thread(thread_id: str, req: _ac.ThreadTitleRequest):
        await db_writes.update_chat_thread_title(thread_id, req.title); return {"ok": True}

    @router.delete("/api/chat/threads/{thread_id}", dependencies=[Depends(_ac.verify_token)])
    async def delete_thread(thread_id: str):
        await db_writes.delete_chat_thread(thread_id); return {"ok": True}

    @router.post("/api/chat", dependencies=[Depends(_ac.verify_token)])
    async def chat_endpoint(req: _ac.ChatRequest, auth_user: _ac.Person = Depends(_ac.verify_token)):
        from llm.chat import chat_general
        import aiohttp
        
        real_name = person_registry.display_name(auth_user.id)
        user = req.person or real_name
        hist = [{"role": m.role, "content": m.content} for m in (req.history or [])]

        # Broadcast typing status
        if req.thread_id:
            await connection_manager.broadcast({"type": "chat.typing", "thread_id": req.thread_id, "status": True})

        try:
            try:
                reply = await asyncio.wait_for(
                    chat_general(
                        req.message, hist, _ac.config, 
                        person_name=user, 
                        triggered_by="web", 
                        model=_ac.config.get("webui_model"), 
                        group=auth_user.role,
                        actor_id=auth_user.id
                    ),
                    timeout=45.0
                )
            except asyncio.TimeoutError:
                raise HTTPException(status_code=504, detail="Bernie is thinking too hard (request timed out).")

            if req.thread_id:
                await db_writes.add_chat_message(req.thread_id, "user", req.message)
                await db_writes.add_chat_message(req.thread_id, "assistant", reply)
                await connection_manager.broadcast({"type": "chat.typing", "thread_id": req.thread_id, "status": False})
            return {"reply": reply}
        except HTTPException:
            if req.thread_id:
                await connection_manager.broadcast({"type": "chat.typing", "thread_id": req.thread_id, "status": False})
            raise
        except Exception as e:
            if req.thread_id:
                await connection_manager.broadcast({"type": "chat.typing", "thread_id": req.thread_id, "status": False})
            log.error(f"Chat error: {e}"); raise HTTPException(status_code=500, detail="Bernie had a minor internal collapse.")

    # --- OpenAI-compatible endpoints (for OpenWebUI) ---


    @router.get("/v1/models", dependencies=[Depends(_ac.verify_bearer_token)])
    async def openai_models():
        return {"object": "list", "data": [{"id": "bernie", "object": "model", "created": int(time.time()), "owned_by": "bernie"}]}

    @router.post("/v1/chat/completions", dependencies=[Depends(_ac.verify_bearer_token)])
    async def openai_completions(req: OAICompletionRequest):
        from llm.chat import chat_general
        log.info(f"[openai-shim] request: user={req.user!r}, messages={len(req.messages)}, stream={req.stream}")

        # Resolve person from email or default
        openwebui_user_map = _ac.config.get("openwebui_users", {})
        person_id = person_registry.resolve(req.user)
        if not person_id and req.user in openwebui_user_map:
            person_id = person_registry.resolve(openwebui_user_map[req.user])
        if not person_id:
            person_id = _ac.config.get("webui_user", "dad")

        person = person_registry.get(person_id) or {}
        person_name = person_registry.display_name(person_id)
        group = person.get("role", "kids")

        if not req.messages:
            raise HTTPException(status_code=400, detail="No messages provided")
        current_message = req.messages[-1].content
        history = [{"role": m.role, "content": m.content} for m in req.messages[:-1] if m.role in ("user", "assistant")]

        try:
            reply = await chat_general(
                current_message, history, _ac.config,
                person_name=person_name, triggered_by="openwebui",
                model=_ac.config.get("openwebui_model") or _ac.config.get("webui_model"), 
                group=group,
                actor_id=person_id,
                openwebui=True,
            )
        except Exception as e:
            log.error(f"[openai-shim] chat error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

        import uuid
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        if req.stream:
            async def event_stream():
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": "bernie",
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": reply}, "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
                stop = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": "bernie",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(stop)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(event_stream(), media_type="text/event-stream")
        else:
            return {"id": cid, "object": "chat.completion", "created": created, "model": "bernie",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


    return router
