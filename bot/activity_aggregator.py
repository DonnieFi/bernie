import asyncio
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path as _Path

import aiohttp

from http_session import get_http_session
from config import config
from db_binding import get_database
import db_writes

_cache = {}
_cache_time = 0

_accounts_cache: dict = {}
_accounts_cache_time: float = 0.0

# Session-title generation is kicked off lazily from get_top_sessions, which the
# dashboard polls repeatedly. Track in-flight session_ids so concurrent dashboard
# refreshes don't spawn duplicate LLM calls for the same session.
_title_inflight: set[str] = set()

def invalidate_cache():
    global _cache, _cache_time, _accounts_cache, _accounts_cache_time
    _cache = {}
    _cache_time = 0
    _accounts_cache = {}
    _accounts_cache_time = 0.0

async def get_openrouter_balance():
    or_keys = config.get("openrouter_keys", [{"env": "OPENROUTER_API_KEY", "label": "primary"}])
    timeout = aiohttp.ClientTimeout(total=6)
    for key_cfg in or_keys:
        env_var = key_cfg.get("env")
        key = os.environ.get(env_var)
        if not key:
            continue
        try:
            s = get_http_session()
            async with s.get("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"}, timeout=timeout) as r:
                    if r.ok:
                        data = (await r.json()).get("data", {})
                        total = data.get("total_credits", 0)
                        usage = data.get("total_usage", 0)
                        return {
                            "balanceRemaining": total - usage,
                            "budget": total
                        }
        except Exception:
            pass
    return {"balanceRemaining": 0, "budget": 0}

async def get_provider_accounts(use_cache: bool = False):
    global _accounts_cache, _accounts_cache_time
    if use_cache and _accounts_cache and (time.time() - _accounts_cache_time < 60):
        return _accounts_cache.get("data", [])

    last_used = await get_database().get_token_last_used()

    # Anthropic: use the most recently billed claude model from the DB
    anthropic_model = last_used.get("anthropic_model")
    if not anthropic_model:
        # Fallback to config active_model if it's a claude model
        active = config.get("active_model", "")
        anthropic_model = active if active.startswith("claude") else "claude-sonnet-5"

    c_cfg = config.get("anthropic_credits", {})
    amt = float(c_cfg.get("amount", 0))
    as_of = c_cfg.get("as_of")
    since = await get_database().get_anthropic_spend_since(as_of) if as_of else 0.0

    anthropic = {
        "provider": "anthropic",
        "activeModel": anthropic_model,
        "balanceRemaining": max(0, amt - since) if as_of else 0,
        "budget": amt,
        "lastToppedUp": {"amount": amt, "at": as_of} if as_of else None,
        "lastUsedAt": last_used.get("anthropic")
    }

    # OpenRouter: use current active_model if it's an or- model; fall back to last DB entry
    active = config.get("active_model", "")
    if active and not active.startswith("claude"):
        or_model = active
    else:
        or_model = last_used.get("other_model") or "—"

    or_balance = await get_openrouter_balance()

    openrouter = {
        "provider": "openrouter",
        "activeModel": or_model,
        "balanceRemaining": or_balance["balanceRemaining"],
        "budget": or_balance["budget"],
        "lastToppedUp": None,
        "lastUsedAt": last_used.get("other")
    }

    result = [anthropic, openrouter]
    _accounts_cache["data"] = result
    _accounts_cache_time = time.time()
    return result

def get_pricing():
    """Return pricing dict keyed by OR model IDs, fragments, and LiteLLM aliases (lowercase).

    This lets the frontend look up pricing by whatever model string the DB stores,
    e.g. 'or-deepseek-v4', 'claude-sonnet-4-6', or 'anthropic/claude-sonnet-4.6'.
    """
    try:
        prices_path = _Path(__file__).parent / "model_prices.json"
        with open(prices_path, "r") as f:
            prices = json.load(f)

        pricing = {}
        for p in prices.get("models", []):
            entry = {
                "inputPerMTok": float(p.get("input_per_1m", 0) or 0),
                "outputPerMTok": float(p.get("output_per_1m", 0) or 0),
            }
            model_id = p.get("or_id", "")
            if model_id:
                pricing[model_id] = entry
            for frag in p.get("fragments", []):
                pricing[frag.lower()] = entry

        for alias, slug in prices.get("_litellm_aliases", {}).items():
            slug_l = slug.lower()
            if slug_l in pricing and alias.lower() not in pricing:
                pricing[alias.lower()] = pricing[slug_l]

        return pricing
    except Exception:
        return {}

async def get_activity_dashboard(period: str = "30d", force_refresh: bool = False):
    global _cache, _cache_time

    now = time.time()
    if not force_refresh and (now - _cache_time < 60) and period in _cache:
        return _cache[period]

    days = 30
    if period == "7d": days = 7
    elif period == "90d": days = 90

    # Load 2× the period so we can compute previous-period deltas.
    # get_provider_accounts includes an external HTTP call (OR balance); cap it
    # so a network hiccup never blocks the whole dashboard.
    db = get_database()
    all_daily_task = asyncio.create_task(db.get_daily_per_model(days * 2))
    sessions_task = asyncio.create_task(db.get_top_sessions(days, 10))
    heatmap_task = asyncio.create_task(db.get_hour_dow_heatmap(days))
    accounts_task = asyncio.create_task(
        asyncio.wait_for(get_provider_accounts(), timeout=10.0)
    )

    try:
        all_rows, sessions, heatmap, accounts = await asyncio.gather(
            all_daily_task, sessions_task, heatmap_task, accounts_task
        )
    except asyncio.TimeoutError:
        accounts = []
        all_rows, sessions, heatmap = await asyncio.gather(
            all_daily_task, sessions_task, heatmap_task
        )

    # Determine current-period cutoff (Halifax local date)
    try:
        from config import TASK_TZ
        now_hfx = datetime.now(timezone.utc).astimezone(TASK_TZ)
        current_cutoff = (now_hfx - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    except Exception:
        from datetime import date
        current_cutoff = (date.today() - timedelta(days=days - 1)).isoformat()

    current_rows = [r for r in all_rows if r["date"] >= current_cutoff]
    prev_rows = [r for r in all_rows if r["date"] < current_cutoff]

    # Build daily output from current period
    daily_grouped = defaultdict(dict)
    for r in current_rows:
        daily_grouped[r["date"]][r["model"]] = {
            "inTok": r["in_tok"],
            "outTok": r["out_tok"],
            "cacheTok": r["cache_read_tok"],
            "cacheCreateTok": r["cache_creation_tok"],
            "requests": r["requests"],
            "cost": r["cost"]
        }

    # Backfill every calendar day in the period with empty models so the
    # frontend chart never jumps over gaps (zero-usage days must be explicit).
    all_period_dates = set()
    try:
        from config import TASK_TZ
        _now_hfx = datetime.now(timezone.utc).astimezone(TASK_TZ)
        for i in range(days):
            all_period_dates.add((_now_hfx - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d"))
    except Exception:
        from datetime import date as _date
        base = _date.today()
        for i in range(days):
            all_period_dates.add((base - timedelta(days=days - 1 - i)).isoformat())
    for d in all_period_dates:
        if d not in daily_grouped:
            daily_grouped[d] = {}

    daily = [{"date": d, "models": m} for d, m in sorted(daily_grouped.items())]

    # Previous period aggregate for delta tiles
    prev_summary = {
        "cost": sum(r["cost"] for r in prev_rows),
        "requests": sum(r["requests"] for r in prev_rows),
        "tokens": sum(r["in_tok"] + r["out_tok"] for r in prev_rows),
    }

    response = {
        "period": period,
        "lastSync": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "daily": daily,
        "prevSummary": prev_summary,
        "accounts": accounts,
        "topSessions": sessions,
        "heatmap": heatmap,
        "pricing": get_pricing()
    }

    _cache[period] = response
    _cache_time = now

    return response


_TITLE_SYSTEM = (
    "You generate conversation titles. Return ONLY a 3-5 word plain-text title "
    "capturing the core topic (e.g. 'Caddy Proxy Debugging', 'Meal Prep Query'). "
    "No quotes, no markdown, no punctuation, no explanation."
)


async def generate_and_cache_session_title(session_id: str, channel_id: int, start_time: float):
    """Direct single-turn LLM call to generate a concise title and cache it.

    Bypasses chat_general / the full agentic pipeline intentionally — no Bernie
    persona, no tools, no calendar/memory context needed for a 3-5 word title.
    Follows the same pattern as call_for_audit.
    """
    import logging
    log = logging.getLogger(__name__)
    from llm.clients import make_client, close_client
    from llm.runtime import get_container
    from config import config
    from datetime import datetime, timezone as dt_timezone

    if session_id in _title_inflight:
        return
    db = get_database()
    if await db.get_cached_session_title(session_id):
        return
    _title_inflight.add(session_id)

    start_dt = datetime.fromtimestamp(start_time, tz=dt_timezone.utc).isoformat()
    end_dt = datetime.fromtimestamp(start_time + 3600, tz=dt_timezone.utc).isoformat()

    try:
        snippet = await db.conversation_snippet_for_title(
            channel_id, start_dt, end_dt, limit=3
        )
        if not snippet:
            return

        transcript = "\n".join([f"{r['role']}: {r['content'][:200]}" for r in snippet])
        prompt = f"Conversation snippet:\n{transcript}"

        model = config.get("eval", {}).get("worker_model", "claude-haiku-4-5-20251001")
        if model.startswith("claude-"):
            base_url = None
        elif model in config.get("ollama_models", []):
            base_url = config.get("ollama_base_url", "http://192.168.1.X:11434")  # placeholder; set in config.json
        else:
            base_url = config.get("litellm_base_url", "https://litellm.example.local")

        client = make_client(base_url)
        try:
            resp = await client.messages.create(
                model=model,
                max_tokens=20,
                system=_TITLE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            title_raw = resp.content[0].text
            try:
                await db_writes.routed("log_token_usage", 
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                    model=model,
                    triggered_by="system",
                    cache_creation_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
                    cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                )
            except Exception:
                pass
        finally:
            await close_client(client, get_container())

        clean_title = title_raw.strip().strip('"').strip("'").strip()
        if clean_title:
            await db_writes.routed("cache_session_title", session_id, clean_title)
            log.info(f"Smart session title generated for {session_id}: '{clean_title}'")

    except Exception as e:
        log.error(f"Failed to generate smart session title for {session_id}: {e}")
    finally:
        _title_inflight.discard(session_id)
