"""database.usage — domain module (8lx.1 Phase 1)."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import json as _json
import pathlib as _pathlib
import sqlite_async

from database.conn import (
    HFX,
    _db_conn,
    _db_read,
    _get_connection,
    _get_init_lock,
    _get_lock,
    _pkg,
    _resolve_db_path,
    close_db,
    db_conn,
    wal_checkpoint_passive,
)

log = logging.getLogger("database.usage")

async def log_token_usage(
    input_tokens: int,
    output_tokens: int,
    model: str,
    conversation_id: str = None,
    triggered_by: str = "discord",
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    session_id: str | None = None,
    surface: str = "discord",
):
    """Log a token spend row. surface distinguishes primary family traffic ('discord')
    from eval shadows for cost accounting (perf instrumentation, locked defaults)."""
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO token_usage (
                   input_tokens, output_tokens, model, conversation_id, triggered_by,
                   cache_creation_tokens, cache_read_tokens, session_id, surface
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                input_tokens, output_tokens, model, conversation_id, triggered_by,
                int(cache_creation_tokens or 0), int(cache_read_tokens or 0), session_id,
                surface or "discord",
            ),
        )
        await db.commit()

async def store_shadow_triplet(
    *,
    primary_response: str,
    model_shadow_response: str,
    harness_shadow_response: str,
    shadow_model: str,
    primary_model: str,
    prompt_hash: str,
    channel_id: str,
    actor_id: str,
    user_message: str,
    surface: str = "chat",
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    duration_ms: int | None = None,
    cost_usd: float | None = None,
) -> int:
    """Store a three-call shadow triplet and return the shadow_calls row id."""
    async with _db_conn() as db:
        cur = await db.execute("""
            INSERT INTO shadow_calls
              (primary_model, shadow_model, prompt_hash, primary_response, shadow_response,
               channel_id, actor_id, user_message, tokens_in, tokens_out,
               duration_ms, cost_usd, executor, surface,
               harness_shadow_response, harness_executor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'native', ?, ?, 'smol')
        """, (
            primary_model, shadow_model, prompt_hash, primary_response, model_shadow_response,
            channel_id, actor_id, user_message, tokens_in, tokens_out,
            duration_ms, cost_usd, surface,
            harness_shadow_response,
        ))
        await db.commit()
        return cur.lastrowid

async def store_shadow_judgment(
    *,
    request_id: int,
    judge_kind: str,
    winner: str | None,
    scores: dict | None,
    judge_model: str | None = None,
    actor_id: str | None = None,
) -> None:
    # Retry on transient SQLite contention. The nightly eval worker
    # sometimes loses to checkpoint/dashboard writers and dies on a
    # single locked write — that drops one judgment and cascades into
    # an activity_log lock-error of its own. Three retries with linear
    # back-off (1s, 2s, 3s) is well within the 30s busy_timeout budget.
    import asyncio as _asyncio
    import json
    payload = (request_id, judge_kind, winner,
               json.dumps(scores) if scores else None,
               judge_model, actor_id)
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            async with _db_conn() as db:
                await db.execute("""
                    INSERT INTO shadow_judgments (request_id, judge_kind, winner, scores, judge_model, actor_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, payload)
                await db.commit()
            return
        except Exception as exc:
            last_exc = exc
            if "database is locked" not in str(exc).lower() or attempt == 3:
                raise
            log.warning("store_shadow_judgment: lock (attempt %d/3) — retrying", attempt + 1)
            await _asyncio.sleep(1 + attempt)
    if last_exc:
        raise last_exc

async def get_unscored_triplets(before_date: str) -> list[dict]:
    """Return shadow_calls rows strictly before before_date not yet scored by the LLM judge.

    Uses the same < boundary as get_unscored_shadow_calls so both scoring passes
    cover the same set of rows and older unscored calls are not silently skipped.
    """
    async with _db_read() as db:
        async with db.execute("""
            SELECT sc.* FROM shadow_calls sc
            LEFT JOIN shadow_judgments sj
              ON sj.request_id = sc.id AND sj.judge_kind = 'llm'
            WHERE sc.created_at < ?
              AND sc.harness_shadow_response IS NOT NULL
              AND sj.id IS NULL
        """, (f"{before_date}T00:00:00Z",)) as cur:
            return [dict(row) async for row in cur]

async def get_tool_calls_for_prompt_hash(prompt_hash: str, surface: str) -> list[str]:
    """Return tool names called during a shadow triplet leg. Best-effort — returns [] on miss.

    Matches the explicit `shadow_leg=primary|harness` key written by
    `ToolGateway._emit_activity`, rather than f-string'd Python bools.
    """
    if not prompt_hash:
        return []
    leg = "harness" if surface == "harness" else "primary"
    async with _db_read() as db:
        async with db.execute("""
            SELECT description FROM activity_log
            WHERE event_type = 'tool_call'
              AND metadata LIKE ?
              AND metadata LIKE ?
            ORDER BY id DESC LIMIT 10
        """, (f"%shadow_leg={leg}%", f"%{prompt_hash}%")) as cur:
            rows = [row[0] async for row in cur]
    # Parse "Tool <b>name</b> called..." format
    import re
    names = []
    for row in rows:
        m = re.search(r'<b>(\w+)</b>', row)
        if m:
            names.append(m.group(1))
    return names

async def get_divergent_unsampled_triplets(date_str: str) -> list[dict]:
    """Return triplets where primary and harness_shadow diverged (judge scored) but no HITL yet."""
    async with _db_read() as db:
        async with db.execute("""
            SELECT sc.*, sj.winner as judge_winner, sj.scores as judge_scores
            FROM shadow_calls sc
            JOIN shadow_judgments sj ON sj.request_id = sc.id AND sj.judge_kind = 'llm'
            LEFT JOIN shadow_judgments hitl ON hitl.request_id = sc.id AND hitl.judge_kind = 'hitl'
            WHERE sc.created_at LIKE ?
              AND sj.winner != 'primary'
              AND hitl.id IS NULL
              AND sc.harness_shadow_response IS NOT NULL
        """, (f"{date_str}%",)) as cur:
            return [dict(row) async for row in cur]

async def get_hitl_pending_by_message(message_id: int) -> dict | None:
    async with _db_read() as db:
        async with db.execute("""
            SELECT * FROM shadow_judgments
            WHERE judge_kind = 'hitl_pending'
              AND CAST(json_extract(scores, '$.dm_message_id') AS INTEGER) = ?
        """, (message_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

async def get_shadow_call_count_today(date_str: str) -> int:
    """Return count of shadow calls fired today (YYYY-MM-DD in UTC)."""
    async with _db_read() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM shadow_calls WHERE created_at LIKE ?",
            (f"{date_str}%",)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def store_shadow_call(
    shadow_model: str,
    prompt_hash: str,
    primary_response: str,
    shadow_response: str,
    channel_id: str,
    actor_id: str,
    primary_trace_id: str | None = None,
    user_message: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    duration_ms: int | None = None,
    cost_usd: float | None = None,
) -> int:
    """Insert a shadow call record. Returns the new row id."""
    async with _db_conn() as db:
        cur = await db.execute(
            """INSERT INTO shadow_calls
               (primary_trace_id, shadow_model, prompt_hash,
                primary_response, shadow_response, channel_id, actor_id,
                user_message, tokens_in, tokens_out, duration_ms, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (primary_trace_id, shadow_model, prompt_hash,
             (primary_response or "")[:1500], (shadow_response or "")[:1500],
             channel_id, actor_id,
             (user_message or "")[:500], tokens_in, tokens_out, duration_ms, cost_usd)
        )
        await db.commit()
        return cur.lastrowid

async def get_unscored_shadow_calls(before_date: str) -> list[dict]:
    """Return all unscored shadow_calls strictly before before_date (YYYY-MM-DD).

    Using a strict "less than" boundary instead of an exact date match ensures
    the nightly worker can recover calls that were skipped on previous nights
    (e.g. due to API errors or the max_scored_per_night cap).
    """
    async with _db_read() as db:

        async with db.execute(
            """SELECT id, primary_response, shadow_response, shadow_model,
                      user_message, tokens_in, tokens_out, duration_ms, cost_usd
               FROM shadow_calls
               WHERE created_at < ? AND judge_ran_at IS NULL""",
            (f"{before_date}T00:00:00Z",)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def update_shadow_scores(
    row_id: int,
    primary_intent: float | None,
    primary_tool: float | None,
    shadow_intent: float | None,
    shadow_tool: float | None,
) -> None:
    """Write judge scores back to a shadow_calls row."""
    async with _db_conn() as db:
        await db.execute(
            """UPDATE shadow_calls
               SET primary_score_intent=?, primary_score_tool=?,
                   shadow_score_intent=?, shadow_score_tool=?,
                   judge_ran_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE id=?""",
            (primary_intent, primary_tool, shadow_intent, shadow_tool, row_id)
        )
        await db.commit()

# Hardcoded fallback rates (USD per MTok) when model_prices.json misses a model.
_FALLBACK_RATES: list[tuple[str, float, float]] = [
    ("claude-3-haiku", 0.25, 1.25),
    ("claude-3-opus", 15.0, 75.0),
    ("claude-haiku", 1.0, 5.0),
    ("claude-sonnet", 3.0, 15.0),
    ("claude-opus", 5.0, 25.0),
]
_FALLBACK_DEFAULT = (3.0, 15.0)  # Sonnet-tier USD per MTok


def _load_price_index() -> tuple[dict[str, tuple[float, float]], list[tuple[str, float, float]]]:
    """
    Load model_prices.json → (exact_map, fragment_list).
    exact_map:     {lowercase_key → (input_per_1m, output_per_1m)}
    fragment_list: [(fragment, input_per_1m, output_per_1m)] sorted longest-first
    """
    # File lives next to bot package root (sibling of database/), not inside database/
    prices_path = _pathlib.Path(__file__).resolve().parent.parent / "model_prices.json"
    if not prices_path.exists():
        return {}, []
    try:
        data = _json.loads(prices_path.read_text())
        exact: dict[str, tuple[float, float]] = {}
        frags: list[tuple[str, float, float]] = []

        for entry in data.get("models", []):
            inp = float(entry["input_per_1m"])
            out = float(entry["output_per_1m"])
            rate = (inp, out)
            or_id = entry["or_id"].lower()
            exact[or_id] = rate
            for frag in entry.get("fragments", []):
                fl = frag.lower()
                exact[fl] = rate
                frags.append((fl, inp, out))

        # Resolve LiteLLM aliases (or-deepseek-v3 → deepseek/deepseek-chat → price)
        for alias, slug in data.get("_litellm_aliases", {}).items():
            slug_l = slug.lower()
            if slug_l in exact and alias.lower() not in exact:
                exact[alias.lower()] = exact[slug_l]

        # Sort longest fragment first so more-specific matches win
        frags.sort(key=lambda x: len(x[0]), reverse=True)
        return exact, frags
    except Exception:
        return {}, []


_PRICE_EXACT, _PRICE_FRAGS = _load_price_index()


def reload_model_prices() -> int:
    """Reload model_prices.json without restarting. Returns number of entries loaded."""
    global _PRICE_EXACT, _PRICE_FRAGS
    _PRICE_EXACT, _PRICE_FRAGS = _load_price_index()
    try:
        from openrouter_models import invalidate_alias_table

        invalidate_alias_table()
    except Exception:
        pass
    return len(_PRICE_EXACT)

def _token_cost(
    in_t: int,
    out_t: int,
    model: str,
    *,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> float:
    """Return estimated USD cost for an API call given token counts and model name.

    Anthropic-style cache accounting: cache-creation tokens charge at
    `input_price × 1.25` (the write premium) and cache-read tokens at
    `input_price × 0.1` (the read discount). All four counters are normal
    input/output token counts (`response.usage.input_tokens` already
    excludes cache reads on Anthropic's API).
    """
    m = (model or "").lower()

    def _calc(r_in: float, r_out: float) -> float:
        return (
            in_t * r_in
            + out_t * r_out
            + (cache_creation or 0) * r_in * 1.25
            + (cache_read or 0) * r_in * 0.1
        ) / 1_000_000

    # 1. Exact match from JSON (or resolved alias)
    if m in _PRICE_EXACT:
        r_in, r_out = _PRICE_EXACT[m]
        return _calc(r_in, r_out)

    # 2. Longest-fragment substring match from JSON
    for frag, r_in, r_out in _PRICE_FRAGS:
        if frag in m:
            return _calc(r_in, r_out)

    # 3. Hardcoded fallback table
    for prefix, r_in, r_out in _FALLBACK_RATES:
        if prefix in m:
            return _calc(r_in, r_out)

    r_in, r_out = _FALLBACK_DEFAULT
    return _calc(r_in, r_out)

async def get_token_usage_summary(hours: int = 24) -> dict:
    """Return total tokens and spend for the last N hours."""
    since = (datetime.now(dt_timezone.utc) - timedelta(hours=hours)).isoformat()
    async with _db_read() as db:

        cur = await db.execute("""
            SELECT
                model,
                SUM(input_tokens) as total_input,
                SUM(output_tokens) as total_output,
                SUM(cache_creation_tokens) as total_cc,
                SUM(cache_read_tokens) as total_cr,
                COUNT(*) as request_count
            FROM token_usage
            WHERE logged_at >= ?
            GROUP BY model
        """, (since,))
        rows = await cur.fetchall()

    summary = {}
    for r in rows:
        summary[r["model"]] = {
            "input": r["total_input"],
            "output": r["total_output"],
            "requests": r["request_count"],
            "cost": _token_cost(r["total_input"] or 0, r["total_output"] or 0, r["model"], 
                                cache_creation=r["total_cc"] or 0, cache_read=r["total_cr"] or 0)
        }
    return summary

async def get_token_usage_stats(days: int = 30):
    from collections import defaultdict
    async with _db_read() as db:
        cur = await db.execute(
            """SELECT date(logged_at) as day, model, SUM(input_tokens), SUM(output_tokens),
                      SUM(cache_creation_tokens), SUM(cache_read_tokens)
               FROM token_usage
               WHERE logged_at >= date('now', ?)
               GROUP BY day, model ORDER BY day ASC""",
            (f"-{days} days",)
        )
        rows = await cur.fetchall()

        total_usd = 0.0
        day_map: dict = defaultdict(lambda: {"in": 0, "out": 0, "usd": 0.0})
        for day, model, in_t, out_t, cc_t, cr_t in rows:
            in_t = in_t or 0
            out_t = out_t or 0
            cost = _token_cost(in_t, out_t, model, cache_creation=cc_t or 0, cache_read=cr_t or 0)
            total_usd += cost
            day_map[day]["in"] += in_t
            day_map[day]["out"] += out_t
            day_map[day]["usd"] += cost

        days_data = [{"in": v["in"], "out": v["out"], "usd": round(v["usd"], 6), "day": k} for k, v in sorted(day_map.items())]
        return {"totalUsd": total_usd, "days": days_data}

async def get_daily_per_model(days: int = 90):
    """Per (UTC day, model) token aggregates — SQL GROUP BY (c79.3).

    Day labels use the ISO date prefix of logged_at (UTC), matching
    get_token_usage_stats. Cost is computed once per aggregated row.
    """
    cutoff = (datetime.now(dt_timezone.utc) - timedelta(days=max(1, days) - 1)).strftime("%Y-%m-%d")
    async with _db_read() as db:
        cur = await db.execute(
            """SELECT substr(logged_at, 1, 10) AS day, model,
                      SUM(input_tokens) AS in_tok,
                      SUM(output_tokens) AS out_tok,
                      SUM(cache_creation_tokens) AS cc_tok,
                      SUM(cache_read_tokens) AS cr_tok,
                      COUNT(*) AS reqs
               FROM token_usage
               WHERE logged_at >= datetime('now', ?)
                 AND substr(logged_at, 1, 10) >= ?
               GROUP BY day, model
               ORDER BY day ASC""",
            (f"-{days + 2} days", cutoff),
        )
        rows = await cur.fetchall()

    result = []
    for r in rows:
        day, model = r["day"], r["model"]
        if not day or not model:
            continue
        in_t = r["in_tok"] or 0
        out_t = r["out_tok"] or 0
        cc_t = r["cc_tok"] or 0
        cr_t = r["cr_tok"] or 0
        result.append({
            "date": day,
            "model": model,
            "in_tok": in_t,
            "out_tok": out_t,
            "cache_creation_tok": cc_t,
            "cache_read_tok": cr_t,
            "requests": r["reqs"] or 0,
            "cost": _token_cost(in_t, out_t, model, cache_creation=cc_t, cache_read=cr_t),
        })
    return result

async def get_top_sessions(days: int = 30, limit: int = 10):
    """Top sessions by cost; titles joined in one query (c79.3, no N+1)."""
    from collections import defaultdict
    hfx = HFX

    async with _db_read() as db:
        cur = await db.execute(
            """SELECT session_id, model,
                      SUM(input_tokens) as in_tok, SUM(output_tokens) as out_tok,
                      SUM(cache_creation_tokens) as cc_tok, SUM(cache_read_tokens) as cr_tok,
                      COUNT(*) as reqs, MAX(logged_at) as last_act
               FROM token_usage
               WHERE logged_at >= datetime('now', ?) AND session_id IS NOT NULL
               GROUP BY session_id, model""",
            (f"-{days} days",)
        )
        rows = await cur.fetchall()

    session_costs = defaultdict(lambda: {
        "cost": 0.0, "reqs": 0, "model": None, "last_act": "", "toks": 0,
        "channel_id": None, "start_time": None,
    })
    for r in rows:
        sid, model, in_t, out_t, cc_t, cr_t, reqs, last_act = r
        cost = _token_cost(in_t or 0, out_t or 0, model, cache_creation=cc_t or 0, cache_read=cr_t or 0)
        s = session_costs[sid]
        s["cost"] += cost
        s["reqs"] += reqs
        s["toks"] += (in_t or 0) + (out_t or 0) + (cc_t or 0) + (cr_t or 0)
        if not s["last_act"] or last_act > s["last_act"]:
            s["last_act"] = last_act
            s["model"] = model

        parts = sid.split("-")
        if len(parts) >= 2:
            s["channel_id"] = parts[0]
            try:
                raw_start = int(parts[1])
                if raw_start < 10_000_000:
                    s["start_time"] = raw_start * 1800
                elif raw_start > 10_000_000_000:
                    s["start_time"] = raw_start // 1000
                else:
                    s["start_time"] = raw_start
            except Exception:
                pass

    top = sorted(session_costs.items(), key=lambda x: x[1]["cost"], reverse=True)[:limit]
    sids = [sid for sid, _ in top]

    titles: dict[str, str] = {}
    if sids:
        placeholders = ",".join("?" * len(sids))
        async with _db_read() as db:
            cur = await db.execute(
                f"SELECT session_id, title FROM session_titles WHERE session_id IN ({placeholders})",
                tuple(sids),
            )
            titles = {row[0]: row[1] for row in await cur.fetchall() if row[0]}

    result = []
    for sid, s_data in top:
        title = titles.get(sid)
        if not title:
            fallback_title = "Unknown Session"
            if s_data["channel_id"] and s_data["start_time"]:
                start_dt = datetime.fromtimestamp(s_data["start_time"], tz=dt_timezone.utc).isoformat()
                end_dt = datetime.fromtimestamp(s_data["start_time"] + 3600, tz=dt_timezone.utc).isoformat()
                async with _db_read() as db:
                    cur = await db.execute(
                        """SELECT content FROM conversation_history
                           WHERE channel_id = ? AND role = 'user'
                             AND created_at >= ? AND created_at <= ?
                           ORDER BY created_at ASC LIMIT 1""",
                        (s_data["channel_id"], start_dt, end_dt),
                    )
                    title_row = await cur.fetchone()
                    if title_row and title_row[0]:
                        fallback_title = title_row[0][:50] + ("..." if len(title_row[0]) > 50 else "")
            title = fallback_title
            if s_data["channel_id"] and s_data["start_time"]:
                from activity_aggregator import generate_and_cache_session_title
                asyncio.create_task(
                    generate_and_cache_session_title(sid, int(s_data["channel_id"]), s_data["start_time"])
                )

        dt_str = s_data["last_act"]
        last_ts = 0
        if dt_str:
            dt = datetime.fromisoformat(dt_str.replace(" ", "T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=dt_timezone.utc)
            last_ts = int(dt.timestamp())
            local_act = dt.astimezone(hfx).strftime("%b %d, %H:%M")
        else:
            local_act = ""

        result.append({
            "id": sid,
            "title": title,
            "modelId": s_data["model"],
            "msgs": s_data["reqs"],
            "tokens": s_data["toks"],
            "cost": round(s_data["cost"], 6),
            "lastActivityAt": local_act,
            "lastActivityTs": last_ts,
        })

    return result

async def get_hour_dow_heatmap(days: int = 30):
    hfx = HFX
    cells = [[0]*24 for _ in range(7)]
    
    async with _db_read() as db:
        cur = await db.execute(
            """SELECT logged_at FROM token_usage
               WHERE logged_at >= datetime('now', ?)""",
            (f"-{days} days",)
        )
        rows = await cur.fetchall()
        
    for r in rows:
        dt_str = r[0]
        if not dt_str: continue
        dt = datetime.fromisoformat(dt_str.replace(" ", "T"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=dt_timezone.utc)
        local_dt = dt.astimezone(hfx)
        
        dow = int(local_dt.strftime("%w"))
        hour = local_dt.hour
        cells[dow][hour] += 1
        
    return {"cells": cells}

async def get_anthropic_spend_since(as_of: str) -> float:
    """Return estimated USD spent on claude-* models on or after as_of (YYYY-MM-DD)."""
    async with _db_read() as db:
        cur = await db.execute(
            """SELECT model, SUM(input_tokens), SUM(output_tokens),
                      SUM(cache_creation_tokens), SUM(cache_read_tokens)
               FROM token_usage
               WHERE model LIKE 'claude-%' AND date(logged_at) >= ?
               GROUP BY model""",
            (as_of,)
        )
        rows = await cur.fetchall()
    return sum(_token_cost(r[1] or 0, r[2] or 0, r[0], cache_creation=r[3] or 0, cache_read=r[4] or 0) for r in rows)

async def get_or_spend(days: int = 30) -> float:
    """Return estimated USD spent on non-claude models in last N days."""
    async with _db_read() as db:
        cur = await db.execute(
            """SELECT model, SUM(input_tokens), SUM(output_tokens),
                      SUM(cache_creation_tokens), SUM(cache_read_tokens)
               FROM token_usage
               WHERE model NOT LIKE 'claude-%' AND date(logged_at) >= date('now', ?)
               GROUP BY model""",
            (f"-{days} days",)
        )
        rows = await cur.fetchall()
    return sum(_token_cost(r[1] or 0, r[2] or 0, r[0], cache_creation=r[3] or 0, cache_read=r[4] or 0) for r in rows)

async def get_token_last_used():
    """Return last logged_at timestamp for claude-* models and non-claude models separately."""
    def _iso(ts: str | None) -> str | None:
        if not ts:
            return None
        return ts.replace(" ", "T") + ("Z" if not ts.endswith("Z") else "")

    async with _db_read() as db:
        cur = await db.execute(
            "SELECT logged_at, model FROM token_usage WHERE model LIKE 'claude-%' "
            "ORDER BY logged_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
        anthropic_last = _iso(row[0]) if row else None
        anthropic_model = row[1] if row else None

        cur = await db.execute(
            "SELECT logged_at, model FROM token_usage WHERE model NOT LIKE 'claude-%' "
            "ORDER BY logged_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
        other_last = _iso(row[0]) if row else None
        other_model = row[1] if row else None

    return {
        "anthropic": anthropic_last,
        "anthropic_model": anthropic_model,
        "other": other_last,
        "other_model": other_model,
    }

