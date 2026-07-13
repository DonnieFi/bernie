"""
litellm_service.py — LiteLLM management API wrapper.

Handles adding/removing models dynamically via the LiteLLM admin API.
store_model_in_db: true is set on the server, so changes persist without restarts.

Management API base: config["litellm_admin_url"] (default https://litellm.example.local)
Auth: LTE_LLM_MASTER_KEY env var
TLS: self-signed cert, verification skipped.
"""

import logging
import os
import ssl

import aiohttp

from http_session import get_http_session

from config import config

log = logging.getLogger(__name__)

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# In-memory cache of model_name → cache_mode (populated from LiteLLM model_info)
# This lets model_cache_support() stay synchronous and fast.
_model_cache_modes: dict[str, str] = {}


def _admin_url() -> str:
    return config.get("litellm_admin_url", "https://litellm.example.local").rstrip("/")


def _headers() -> dict:
    key = os.environ.get("LTE_LLM_MASTER_KEY", "")
    return {
        "Authorization": f"Bearer {key}",
        "x-litellm-api-key": key,
        "Content-Type": "application/json",
    }


def _ssl_ctx():
    """Return an SSL context that skips verification (self-signed cert)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def list_models() -> list[dict]:
    """
    Return all models registered in LiteLLM.
    Each dict has at minimum: model_name, model_info (id).
    """
    url = f"{_admin_url()}/model/info"
    try:
        session = get_http_session()
        async with session.get(
                url, headers=_headers(),
                ssl=_ssl_ctx(),
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    log.error(f"litellm list_models: HTTP {resp.status}")
                    return []
                data = await resp.json()
                return data.get("data", [])
    except Exception as e:
        log.error(f"litellm list_models error: {e}")
        return []


async def sync_config_litellm_models() -> list[str]:
    """Sync config.litellm_models to the DB-backed LiteLLM model registry.

    Returns the sorted model names that were found. If LiteLLM returns no model
    names, leave config untouched so a transient admin outage does not wipe the
    available model list.
    """
    models = await list_models()
    names = sorted({
        m.get("model_name")
        for m in models
        if isinstance(m, dict) and m.get("model_name")
    })
    if not names:
        log.warning("litellm model sync skipped: no model names returned")
        return []

    from config import update_config

    await update_config({"litellm_models": names})
    log.info("LiteLLM model list synced from DB: %s", names)
    return names


async def add_openrouter_model(alias: str, openrouter_slug: str, cache_mode: str | None = None) -> tuple[bool, str]:
    """
    Register an OpenRouter model in LiteLLM under the given alias (e.g. 'or-deepseek-v3').

    openrouter_slug is the OpenRouter model ID, e.g. 'deepseek/deepseek-chat'.
    cache_mode: "none" | "auto_provider" | "anthropic" (stored in model_info for later lookup).
    Returns (success, message).
    """
    url = f"{_admin_url()}/model/new"
    model_info = {"description": f"OpenRouter: {openrouter_slug}"}
    if cache_mode in ("none", "auto_provider", "anthropic"):
        model_info["cache_mode"] = cache_mode

    payload = {
        "model_name": alias,
        "litellm_params": {
            "model": openrouter_slug,
            "api_base": OPENROUTER_API_BASE,
            "api_key": "os.environ/OPENROUTER_API_KEY",
            "custom_llm_provider": "openai",
        },
        "model_info": model_info
    }
    try:
        session = get_http_session()
        async with session.post(
            url, headers=_headers(), json=payload,
            ssl=_ssl_ctx(),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
                body = await resp.json()
                if resp.status in (200, 201):
                    model_id = body.get("model_id", "unknown")
                    log.info(f"litellm: added {alias} → {openrouter_slug} (id={model_id})")
                    try:
                        from openrouter_models import register_openrouter_alias

                        register_openrouter_alias(alias, openrouter_slug)
                    except Exception as exc:
                        log.warning(
                            "litellm: registered in LiteLLM but failed to persist "
                            "openrouter_direct alias map for %s: %s",
                            alias,
                            exc,
                        )
                    await _refresh_model_cache_modes()
                    return True, model_id
                log.error(f"litellm add_model: HTTP {resp.status} — {body}")
                return False, body.get("error", {}).get("message", str(body))
    except Exception as e:
        log.error(f"litellm add_openrouter_model error: {e}")
        return False, str(e)


async def delete_model(model_id: str) -> tuple[bool, str]:
    """
    Remove a model from LiteLLM by its model_id (returned when added).
    Returns (success, message).
    """
    url = f"{_admin_url()}/model/delete"
    try:
        session = get_http_session()
        async with session.post(
            url, headers=_headers(), json={"id": model_id},
            ssl=_ssl_ctx(),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
                body = await resp.json()
                if resp.status == 200:
                    log.info(f"litellm: deleted model {model_id}")
                    await _refresh_model_cache_modes()
                    return True, "Deleted."
                log.error(f"litellm delete_model: HTTP {resp.status} — {body}")
                return False, body.get("error", {}).get("message", str(body))
    except Exception as e:
        log.error(f"litellm delete_model error: {e}")
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Model cache_mode caching (for fast synchronous lookup in model_cache_support)
# ─────────────────────────────────────────────────────────────────────────────

async def _refresh_model_cache_modes() -> None:
    """Refresh the in-memory cache of model_name → cache_mode from LiteLLM."""
    global _model_cache_modes
    try:
        models = await list_models()
        new_cache = {}
        for m in models:
            name = m.get("model_name")
            if name:
                mode = (m.get("model_info") or {}).get("cache_mode")
                if mode in ("none", "auto_provider", "anthropic"):
                    new_cache[name] = mode
        _model_cache_modes = new_cache
        log.info(f"Refreshed model cache_mode cache: {len(_model_cache_modes)} models")
    except Exception as e:
        log.warning(f"Failed to refresh model cache_mode cache: {e}")


def get_model_cache_mode(model_name: str) -> str | None:
    """Fast synchronous lookup of stored cache_mode for a model."""
    return _model_cache_modes.get(model_name)


async def research_model_caching(openrouter_slug: str) -> dict:
    """
    Best-effort research of prompt caching support for an OpenRouter model.
    Returns a dict with what we know:
      {
        "supports_prompt_caching": bool | None,
        "source": "openrouter_api" | "heuristic" | "unknown",
        "notes": "..."
      }
    Currently OpenRouter's public /models does not reliably expose this,
    so this is mostly a hook for future improvement + manual notes.
    """
    try:
        session = get_http_session()
        async with session.get(
            OPENROUTER_MODELS_URL,
            timeout=aiohttp.ClientTimeout(total=12)
        ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for m in data.get("data", []):
                        if m.get("id") == openrouter_slug:
                            # Look for any future explicit flag
                            flag = m.get("supports_prompt_caching") or m.get("prompt_caching")
                            supp = m.get("supported_parameters", []) or []
                            has_cache = any("cache" in str(p).lower() for p in supp)
                            return {
                                "supports_prompt_caching": bool(flag) or has_cache,
                                "source": "openrouter_api",
                                "notes": f"Found in catalog. flag={flag}, cache_in_supported_params={has_cache}"
                            }
    except Exception as e:
        log.warning("research_model_caching OpenRouter call failed: %s", e)

    # Heuristic fallback for known families (we can expand this over time)
    known_good = {"qwen/qwen3.7-max", "xiaomi/mimo-v2.5-pro", "deepseek/deepseek-chat"}
    if openrouter_slug in known_good:
        return {
            "supports_prompt_caching": True,
            "source": "heuristic",
            "notes": "Manually marked as supporting provider-side caching (user confirmed)"
        }

    return {
        "supports_prompt_caching": None,
        "source": "unknown",
        "notes": "No data found in OpenRouter catalog and no strong heuristic match."
    }
