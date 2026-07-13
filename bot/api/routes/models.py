"""API routes: models (family-bot-8lx.2 hard-cut)."""
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
import aiohttp
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


def build_models_router(ctx: Any) -> APIRouter:
    """Register models routes; closes over container services via ctx."""
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

    @router.get("/api/config/models", dependencies=[Depends(_ac.verify_token)])
    async def get_models():
        anthropic = [{"id": m, "source": "anthropic"} for m in _ac.config.get("anthropic_models", [])]
        configured_litellm = list(_ac.config.get("litellm_models", []) or [])
        lite_base = _ac.config.get("litellm_base_url", "https://litellm.example.local")
        api_key = os.environ.get("LTE_LLM_MASTER_KEY", "")
        live_litellm = []
        try:
            async with http_session.get(
                f"{lite_base}/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    live_litellm = [m["id"] for m in data.get("data", []) if m.get("id")]
        except Exception:
            live_litellm = []
        litellm = [
            {"id": m, "source": "litellm"}
            for m in sorted(set(configured_litellm) | set(live_litellm))
        ]
        current_model, _ = _ac.get_model_info()
        ollama = [{"id": m, "source": "ollama"} for m in _ac.config.get("ollama_models", [])]
        fallback = _ac.config.get("llm_fallback", {})
        eval_cfg = _ac.config.get("eval", {})
        cog_cfg = _ac.config.get("cognitive_workers", {})
        refl = cog_cfg.get("reflection", {}) or {}
        cons = cog_cfg.get("consolidation", {}) or {}
        research = cog_cfg.get("research", {}) or {}
        study = cog_cfg.get("study_guide", {}) or {}
        return {
            "current": current_model,
            "webui_model": _ac.config.get("webui_model"),
            "openwebui_model": _ac.config.get("openwebui_model") or _ac.config.get("webui_model"),
            "fallback_model": fallback.get("model"),
            "digest_model": _ac.config.get("digest_model"),
            "shadow_model": eval_cfg.get("shadow_model"),
            "worker_model": eval_cfg.get("worker_model"),
            "research_model": research.get("default_model"),
            "research_upgrade_model": research.get("upgrade_model"),
            "study_guide_model": study.get("default_model"),
            "audit_model": _ac.config.get("audit_model"),
            "eval_model": eval_cfg.get("eval_model"),
            "judge_fallback_model": eval_cfg.get("judge_fallback_model"),
            "judge_ollama_fallback": eval_cfg.get("judge_ollama_fallback"),
            "vision_model": _ac.config.get("vision_model"),
            "primary_reliable_model": _ac.config.get("primary_reliable_model"),
            # Workers prefer default_model; config.example historically used "model"
            "reflection_model": refl.get("default_model") or refl.get("model"),
            "consolidation_model": cons.get("default_model") or cons.get("model"),
            "models": anthropic + litellm,
            "ollama_models": ollama,
        }

    @router.patch("/api/config/models", dependencies=[Depends(_ac.verify_token)])
    async def set_model_endpoint(data: Dict[str, str]):
        from llm.model_state import set_model as _set_model
        from config import update_config
        model_id = data.get("model")
        target = data.get("target", "discord")
        if not model_id:
            raise HTTPException(status_code=400, detail="Missing model")
        
        updates = {}
        if target == "webui":
            updates["webui_model"] = model_id
        elif target == "openwebui":
            updates["openwebui_model"] = model_id
        elif target == "fallback":
            if model_id not in _ac.config.get("ollama_models", []):
                raise HTTPException(status_code=400, detail="fallback target accepts Ollama models only")
            updates["llm_fallback"] = {"model": model_id}
        elif target == "digest":
            updates["digest_model"] = model_id
        elif target == "shadow":
            updates["eval"] = {"shadow_model": model_id}
        elif target == "worker":
            updates["eval"] = {"worker_model": model_id}
        elif target == "research":
            updates["cognitive_workers"] = {"research": {"default_model": model_id}}
        elif target == "research_upgrade":
            updates["cognitive_workers"] = {"research": {"upgrade_model": model_id}}
        elif target == "study_guide":
            updates["cognitive_workers"] = {"study_guide": {"default_model": model_id}}
        elif target == "audit":
            updates["audit_model"] = model_id
        elif target == "eval":
            updates["eval"] = {"eval_model": model_id}
        elif target == "judge_fallback":
            if model_id in _ac.config.get("ollama_models", []) or model_id.startswith("claude-"):
                raise HTTPException(status_code=400, detail="judge_fallback accepts LiteLLM models only")
            updates["eval"] = {"judge_fallback_model": model_id}
        elif target == "judge_ollama":
            if model_id not in _ac.config.get("ollama_models", []):
                raise HTTPException(status_code=400, detail="judge_ollama_fallback accepts Ollama models only")
            updates["eval"] = {"judge_ollama_fallback": model_id}
        elif target == "vision":
            if model_id not in _ac.config.get("ollama_models", []):
                raise HTTPException(status_code=400, detail="vision target accepts Ollama models only")
            updates["vision_model"] = model_id
        elif target == "primary_reliable":
            if model_id in _ac.config.get("ollama_models", []):
                raise HTTPException(status_code=400, detail="primary_reliable target accepts Anthropic/LiteLLM models only")
            updates["primary_reliable_model"] = model_id
        elif target == "reflection":
            updates["cognitive_workers"] = {"reflection": {"default_model": model_id}}
        elif target == "consolidation":
            updates["cognitive_workers"] = {"consolidation": {"default_model": model_id}}
        else:
            from model_registry import model_base_url
            base_url = model_base_url(model_id, _ac.config)
            _set_model(model_id, base_url)
            updates["active_model"] = model_id
            
        if not updates:
            return {"ok": True, "model": model_id, "target": target}
            
        await update_config(updates)
        return {"ok": True, "model": model_id, "target": target}

    @router.post("/api/config/models/add", dependencies=[Depends(_ac.verify_token)])
    async def add_model_endpoint(data: Dict[str, str]):
        from litellm_service import add_openrouter_model
        from config import update_config
        alias = data.get("alias", "").strip()
        openrouter_slug = data.get("openrouter_slug", "").strip()
        if not alias or not openrouter_slug:
            raise HTTPException(status_code=400, detail="Missing alias or openrouter_slug")
        if not alias.startswith("or-"):
            alias = f"or-{alias}"
        ok, result = await add_openrouter_model(alias, openrouter_slug)
        if not ok:
            raise HTTPException(status_code=502, detail=result)
        models = _ac.config.get("litellm_models", [])
        if alias not in models:
            models.append(alias)
            await update_config({"litellm_models": sorted(models)})
        return {"ok": True, "alias": alias, "model_id": result}

    @router.delete("/api/config/models/{model_id}", dependencies=[Depends(_ac.verify_token)])
    async def remove_model_endpoint(model_id: str):
        from litellm_service import delete_model
        from config import update_config
        ok, msg = await delete_model(model_id)
        if not ok:
            raise HTTPException(status_code=502, detail=msg)
        configured = _ac.config.get("litellm_models", []) or []
        if model_id in configured:
            await update_config({"litellm_models": [m for m in configured if m != model_id]})
        return {"ok": True}


    return router
