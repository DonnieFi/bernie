"""API routes: models (family-bot-8lx.2 hard-cut)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import logging
import os
import aiohttp

import api.common as _ac

log = logging.getLogger(__name__)


def build_models_router(ctx: Any) -> APIRouter:
    """Register models routes; closes over container services via ctx."""
    router = APIRouter()
    http_session = ctx.http_session
    if not hasattr(ctx, "login_attempts"):
        ctx.login_attempts = {}

    @router.get("/api/config/models", dependencies=[Depends(_ac.verify_token)])
    async def get_models():
        from model_catalog import catalog_as_dicts, build_catalog
        from subscription_complete import fetch_runner_health

        # Live LiteLLM discovery (merge with configured aliases).
        configured_litellm = list(_ac.config.get("litellm_models", []) or [])
        lite_base = _ac.config.get("litellm_base_url", "https://litellm.example.local")
        api_key = os.environ.get("LTE_LLM_MASTER_KEY", "")
        live_litellm: list[str] = []
        try:
            sess = http_session
            if sess is not None:
                async with sess.get(
                    f"{lite_base}/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        live_litellm = [m["id"] for m in data.get("data", []) if m.get("id")]
        except Exception:
            live_litellm = []

        health = await fetch_runner_health(_ac.config)
        catalog = catalog_as_dicts(
            _ac.config,
            runner_health=health,
            extra_litellm_ids=live_litellm,
        )
        # Target-filtered pools for web settings (provider-aware).
        pools = {
            target: catalog_as_dicts(
                _ac.config,
                target=target,
                runner_health=health,
                extra_litellm_ids=live_litellm,
            )
            for target in (
                "active", "webui", "openwebui", "digest", "fallback", "shadow",
                "worker", "research", "research_upgrade", "study_guide", "audit",
                "eval", "judge_fallback", "judge_ollama", "vision",
                "primary_reliable", "reflection", "consolidation",
            )
        }

        anthropic = [c for c in catalog if c["provider"] == "anthropic"]
        # Web contract: source litellm for LiteLLM/OpenRouter aliases + live-only
        litellm_rows = [
            {**c, "source": "litellm" if c["provider"] in ("litellm", "openrouter") else c["provider"]}
            for c in catalog
            if c["provider"] in ("litellm", "openrouter")
        ]
        # Ensure live-only ids appear with source litellm
        for mid in live_litellm:
            if not any(r["id"] == mid for r in litellm_rows):
                litellm_rows.append({
                    "id": mid, "model": mid, "provider": "litellm", "source": "litellm",
                    "capabilities": ["text", "tools", "litellm-only", "judge", "structured-output"],
                    "readiness": "unknown", "enabled": True, "fallback_chain": [],
                    "display": f"{mid} (litellm)",
                })
        ollama = [{**c, "source": "ollama"} for c in catalog if c["provider"] == "ollama"]
        subscription = [c for c in catalog if c["provider"] in ("codex", "grok")]

        anthropic_legacy = [{**c, "source": "anthropic"} for c in anthropic]
        current_model, _ = _ac.get_model_info()
        by_id = {
            e.model: e for e in build_catalog(
                _ac.config, runner_health=health, extra_litellm_ids=live_litellm
            )
        }
        current_entry = by_id.get(current_model)
        fallback = _ac.config.get("llm_fallback", {})
        eval_cfg = _ac.config.get("eval", {})
        cog_cfg = _ac.config.get("cognitive_workers", {})
        refl = cog_cfg.get("reflection", {}) or {}
        cons = cog_cfg.get("consolidation", {}) or {}
        research = cog_cfg.get("research", {}) or {}
        study = cog_cfg.get("study_guide", {}) or {}
        return {
            "current": current_model,
            "current_provider": current_entry.provider if current_entry else None,
            "current_readiness": current_entry.readiness if current_entry else None,
            "current_fallback_chain": list(current_entry.fallback_chain) if current_entry else [],
            "subscription_providers": (health or {}).get("providers") or {},
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
            "reflection_model": refl.get("default_model") or refl.get("model"),
            "consolidation_model": cons.get("default_model") or cons.get("model"),
            "models": anthropic_legacy + litellm_rows + subscription,
            "ollama_models": ollama,
            "catalog": catalog,
            "pools": pools,
        }

    @router.patch("/api/config/models", dependencies=[Depends(_ac.verify_token)])
    async def set_model_endpoint(data: Dict[str, str]):
        from llm.model_state import set_model as _set_model
        from config import update_config
        model_id = data.get("model")
        target = data.get("target", "discord")
        if not model_id:
            raise HTTPException(status_code=400, detail="Missing model")

        from model_catalog import validate_model_for_target
        # Validate every target (unknown + incompatible).
        err = validate_model_for_target(
            model_id, target if target != "discord" else "active", _ac.config
        )
        if err:
            raise HTTPException(status_code=400, detail=err)

        updates: dict = {}
        if target == "webui":
            updates["webui_model"] = model_id
        elif target == "openwebui":
            updates["openwebui_model"] = model_id
        elif target == "fallback":
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
            updates["eval"] = {"judge_fallback_model": model_id}
        elif target == "judge_ollama":
            updates["eval"] = {"judge_ollama_fallback": model_id}
        elif target == "vision":
            updates["vision_model"] = model_id
        elif target == "primary_reliable":
            updates["primary_reliable_model"] = model_id
        elif target == "reflection":
            updates["cognitive_workers"] = {"reflection": {"default_model": model_id}}
        elif target == "consolidation":
            updates["cognitive_workers"] = {"consolidation": {"default_model": model_id}}
        else:
            from model_registry import model_base_url, model_source
            source = model_source(model_id, _ac.config)
            base_url = None if source in ("anthropic", "codex", "grok") else model_base_url(model_id, _ac.config)
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
