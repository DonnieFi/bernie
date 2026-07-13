"""Admin / system / LiteLLM / Langfuse tool handlers."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from tools import ROLE_ADMIN, ROLE_ALL, ROLE_BERNIE, tool
from slash_registry import get_all_slash_commands

log = logging.getLogger(__name__)


@tool(
    name="config_doctor",
    description=(
        "Scan runtime config for hygiene issues (CORS wildcard, placeholder tokens, "
        "core schema). Uses the same validate_config as /reload (family-bot-5hy.1)."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ADMIN,
    tier=1,
)
async def handle_config_doctor(args: dict, ctx) -> str:
    from config import config
    from config_validate import validate_config

    findings = validate_config(config)
    if not findings:
        return "config_doctor: no findings."
    lines = ["config_doctor findings:"]
    for f in findings:
        lines.append(f"- [{f.get('severity')}] {f.get('code')}: {f.get('message')}")
    return "\n".join(lines)


@tool(
    name="get_usage_costs",
    description=(
        "Report LLM usage costs from the local token_usage DB (not Langfuse). "
        "Answers 'how much did last week cost?', spend by model, and top sessions. "
        "Uses Bernie's own logged tokens and price table."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Lookback window in days (default 7, max 90)",
            },
            "include_sessions": {
                "type": "boolean",
                "description": "If true, include top expensive sessions (default false)",
            },
        },
    },
    role_required=ROLE_ALL,
    tier=1,
    domain="admin",
)
async def handle_get_usage_costs(args: dict, ctx) -> str:
    """family-bot-5hy.2 / Hermes U9 — agent-queryable cost rollups."""
    from db_binding import get_database

    days = max(1, min(int(args.get("days") or 7), 90))
    include_sessions = bool(args.get("include_sessions"))
    db = get_database()

    stats = await db.get_token_usage_stats(days=days)
    total = float(stats.get("totalUsd") or 0.0)
    day_rows = stats.get("days") or []

    hours = min(days * 24, 24 * 90)
    by_model = await db.get_token_usage_summary(hours=hours)

    lines = [
        f"LLM spend (last {days}d, local token_usage): **${total:.4f}**",
    ]
    if day_rows:
        # recent days first
        recent = list(reversed(day_rows[-7:]))
        lines.append("By day (up to last 7):")
        for d in recent:
            lines.append(
                f"  • {d.get('day')}: ${float(d.get('usd') or 0):.4f} "
                f"({int(d.get('in') or 0):,} in / {int(d.get('out') or 0):,} out)"
            )

    if by_model:
        ranked = sorted(
            by_model.items(),
            key=lambda kv: float(kv[1].get("cost") or 0),
            reverse=True,
        )
        lines.append("By model:")
        for model, info in ranked[:12]:
            lines.append(
                f"  • {model or '?'}: ${float(info.get('cost') or 0):.4f} "
                f"({int(info.get('requests') or 0)} req, "
                f"{int(info.get('input') or 0):,}+{int(info.get('output') or 0):,} tok)"
            )

    if include_sessions:
        try:
            top = await db.get_top_sessions(days=days, limit=5)
        except Exception as e:
            log.warning("get_usage_costs sessions: %s", e)
            top = []
        if top:
            lines.append("Top sessions:")
            for s in top[:5]:
                if isinstance(s, dict):
                    title = (s.get("title") or s.get("id") or "?")[:48]
                    lines.append(
                        f"  • {title}: ${float(s.get('cost') or 0):.4f} "
                        f"model={s.get('modelId') or s.get('model') or '?'}"
                    )
                else:
                    lines.append(f"  • {s}")

    if len(lines) == 1:
        lines.append("(no token_usage rows in this window)")
    return "\n".join(lines)


@tool(
    name="trigger_system_audit",
    description=(
        "Triggers the Watchman nightly system health report immediately and "
        "emails it to the admins."
    ),
    is_write=True,
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ADMIN,
    tier=3,
)
async def handle_trigger_system_audit(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called trigger_system_audit({args})]"
    try:
        from watchman import get_watchman
        wm = get_watchman()
        if not hasattr(wm, "_active_tasks"):
            wm._active_tasks = set()
        task = asyncio.create_task(wm.run_and_email())
        wm._active_tasks.add(task)
        task.add_done_callback(wm._active_tasks.discard)
        return "System audit triggered and will be emailed shortly."
    except Exception as e:
        return f"Failed to trigger audit: {e}"


# ── LiteLLM ──────────────────────────────────────────────────────────────────
@tool(
    name="litellm_list_models",
    description="List all models currently registered in LiteLLM.",
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ADMIN,
    tier=1,
)
async def handle_litellm_list_models(args: dict, ctx) -> str:
    from litellm_service import list_models as _litellm_list
    models = await _litellm_list()
    if not models:
        return "No models registered in LiteLLM (or LiteLLM is unreachable)."
    lines = []
    for m in models:
        name = m.get("model_name", "?")
        info = m.get("model_info", {})
        mid = info.get("id", "?")
        lines.append(f"• `{name}` (id: {mid})")
    return "Registered LiteLLM models:\n" + "\n".join(lines)


@tool(
    name="litellm_add_model",
    description="Register a new OpenRouter model in LiteLLM. Alias must start with 'or-'. Optional cache_mode: none | auto_provider | anthropic. If omitted, we try a quick research lookup.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "alias":           {"type": "string", "description": "Bernie alias, e.g. or-deepseek-v3"},
            "openrouter_slug": {"type": "string", "description": "OpenRouter model ID"},
            "cache_mode": {
                "type": "string",
                "enum": ["none", "auto_provider", "anthropic"],
                "description": "Prompt caching behaviour. 'auto_provider' = server-side (like current or-gpt-5-4-mini entries). 'anthropic' = we send cache_control."
            },
        },
        "required": ["alias", "openrouter_slug"],
    },
    role_required=ROLE_ADMIN,
    tier=3,
)
async def handle_litellm_add_model(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called litellm_add_model({args})]"
    from litellm_service import add_openrouter_model as _litellm_add

    alias = args["alias"]
    openrouter_slug = args["openrouter_slug"]
    cache_mode = args.get("cache_mode")
    if not alias.startswith("or-"):
        alias = f"or-{alias}"

    # If user didn't specify, try a quick research pass
    if not cache_mode:
        try:
            from litellm_service import research_model_caching
            research = await research_model_caching(openrouter_slug)
            if research.get("supports_prompt_caching") is True:
                cache_mode = "auto_provider"
        except Exception:
            pass

    ok, result = await _litellm_add(alias, openrouter_slug, cache_mode=cache_mode)
    if ok:
        models = ctx.config.get("litellm_models", [])
        if alias not in models:
            models.append(alias)
            from config import update_config
            await update_config({"litellm_models": sorted(models)})
        from llm.clients import model_cache_support
        note = model_cache_support(alias)
        return (
            f"Registered `{alias}` → `{openrouter_slug}` (model_id: `{result}`). "
            f"{note} Switch with `/model {alias}`."
        )
    return f"Failed to add model: {result}"


@tool(
    name="litellm_remove_model",
    description="Remove a model from LiteLLM. Accepts either the internal id or the friendly alias (or-xxx).",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "model_id": {"type": "string", "description": "Internal LiteLLM id or friendly alias (e.g. or-qwen-37)"},
        },
        "required": ["model_id"],
    },
    role_required=ROLE_ADMIN,
    tier=3,
)
async def handle_litellm_remove_model(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called litellm_remove_model({args})]"

    from litellm_service import delete_model as _litellm_delete, list_models

    raw = args["model_id"]

    # If it looks like an internal id (long hex), use it directly
    if len(raw) > 30 and "-" not in raw:
        model_id = raw
    else:
        # Try to resolve friendly alias to the real LiteLLM internal id
        models = await list_models()
        model_id = None
        for m in models:
            if m.get("model_name") == raw:
                model_id = (m.get("model_info") or {}).get("id") or m.get("id")
                break
        if not model_id:
            return f"Could not find model '{raw}' in LiteLLM. Pass the internal id (from /model/info) or the exact alias."

    ok, msg = await _litellm_delete(model_id)
    if ok:
        configured = ctx.config.get("litellm_models", [])
        if raw in configured:
            from config import update_config
            await update_config({"litellm_models": [m for m in configured if m != raw]})
        return f"Removed `{raw}` (id: {model_id}) from LiteLLM."
    return f"Failed to remove model: {msg}"


@tool(
    name="litellm_diagnose_caching",
    description="Diagnostic: show current caching status for a registered LiteLLM model + run research on the underlying OpenRouter slug.",
    is_write=False,
    input_schema={
        "type": "object",
        "properties": {
            "alias": {"type": "string", "description": "Registered alias e.g. or-qwen-37"},
        },
        "required": ["alias"],
    },
    role_required=ROLE_ADMIN,
    tier=1,
)
async def handle_litellm_diagnose_caching(args: dict, ctx) -> str:
    if ctx.shadow:
        return "[shadow] would have diagnosed caching"

    alias = args["alias"]
    from litellm_service import list_models, research_model_caching
    from llm.clients import model_cache_support

    models = await list_models()
    stored = None
    description = None
    for m in models:
        if m.get("model_name") == alias:
            info = m.get("model_info") or {}
            stored = info.get("cache_mode")
            description = info.get("description")
            break

    report = [f"**Caching diagnosis for `{alias}`**"]
    report.append(f"UI note: {model_cache_support(alias)}")
    report.append(f"Stored in LiteLLM: cache_mode={stored or 'not set'}")

    slug = None
    if description and "OpenRouter:" in description:
        slug = description.split("OpenRouter:", 1)[1].strip()

    if slug:
        research = await research_model_caching(slug)
        report.append(f"OpenRouter slug: {slug}")
        report.append(f"Research result: {research}")
    else:
        report.append("Could not determine OpenRouter slug from registration.")

    return "\n".join(report)


@tool(
    name="litellm_switch_model",
    description=(
        "Switch Bernie's active model. Use a registered LiteLLM alias or a "
        "Claude model name. Changes take effect immediately and persist."
    ),
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "model_name": {"type": "string", "description": "Model alias or Claude name, or 'reset'"},
        },
        "required": ["model_name"],
    },
    role_required=ROLE_ADMIN,
    tier=3,
)
async def handle_litellm_switch_model(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called litellm_switch_model({args})]"
    from model_registry import DEFAULT_MODEL
    from llm.model_state import set_model

    name = args["model_name"].strip()
    if not name:
        return "❌ Error: Model name cannot be empty."

    if name == "reset":
        from model_registry import reset_model_from_config
        name = reset_model_from_config(ctx.config, DEFAULT_MODEL)

    valid_models = set([DEFAULT_MODEL])
    if ctx.config:
        valid_models.update(ctx.config.get("anthropic_models", []))
        valid_models.update(ctx.config.get("litellm_models", []))
        valid_models.update(ctx.config.get("ollama_models", []))
        if "default_model" in ctx.config:
            valid_models.add(ctx.config["default_model"])

    if name not in valid_models:
        return (
            f"❌ Error: Model `{name}` is not registered.\n"
            f"Please register the model first, or choose one of the following:\n"
            f"• Anthropic: {', '.join(ctx.config.get('anthropic_models', [])) or 'None'}\n"
            f"• LiteLLM: {', '.join(ctx.config.get('litellm_models', [])) or 'None'}\n"
            f"• Ollama: {', '.join(ctx.config.get('ollama_models', [])) or 'None'}"
        )

    from model_registry import model_base_url
    base_url = model_base_url(name, ctx.config)
    set_model(name, base_url)
    from config import update_config
    await update_config({"active_model": name})
    from llm.clients import model_cache_support
    return f"Switched to `{name}`. {model_cache_support(name)}"


@tool(
    name="reset_web_pin",
    description=(
        "Generate a new web UI login password for a family member and send "
        "it by Discord DM."
    ),
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "Family member name"},
        },
        "required": ["person"],
    },
    role_required=ROLE_ADMIN,
    tier=3,
)
async def handle_reset_web_pin(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called reset_web_pin({args})]"
    from auth_service import generate_pin, hash_pin
    from constants import registry as person_registry

    person_id = person_registry.resolve(args["person"])
    person = person_registry.get(person_id) if person_id else None
    if not person:
        return f"No family member named '{args['person']}'."

    display_name = person["display"]
    discord_id = person.get("discord_id")
    if not discord_id:
        return f"{display_name} doesn't have a discord_id set in config. Add one first."

    password = generate_pin()
    hashed_pwd = hash_pin(password)
    from config import update_config
    await update_config({"family_members": {display_name: {"web_pin_hash": hashed_pwd}}})

    try:
        if not ctx.services.orchestrator:
            raise RuntimeError("Notification orchestrator unavailable")

        await ctx.services.orchestrator.notify(
            ctx.services.orchestrator.notification(
                recipient_id=str(discord_id),
                message=(
                    f"Hi {display_name},\n\nYour new Bernie Web UI password is: `{password}`\n\n"
                    "Use this to log in at the family dashboard. Keep it to yourself!\n\n— Bernie"
                )
            )
        )
        return f"Done — new password generated and DMed to {display_name} on Discord."
    except Exception as e:
        return (
            f"Password was reset and saved, but failed to send the Discord DM: {e}. "
            "Reset it again when DM delivery is available."
        )


@tool(
    name="reload_config",
    description="Reload config.json from disk. Use when config has been manually edited.",
    is_write=True,
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ADMIN,
    tier=3,
)
async def handle_reload_config(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called reload_config({args})]"
    try:
        from config import reload_config as _reload
        _reload()

        # Refresh LiteLLM model cache (including cache_mode) after reload
        try:
            from litellm_service import _refresh_model_cache_modes
            asyncio.create_task(_refresh_model_cache_modes())
        except Exception:
            pass

        return "Configuration reloaded successfully."
    except Exception as e:
        return f"Failed to reload configuration: {e}"


# ── System health / logs ────────────────────────────────────────────────────
@tool(
    name="get_system_health",
    description=(
        "Real-time audit of the household infrastructure (BernieHost host). "
        "Returns Docker container status, Pi-hole heartbeats, and Bernie's "
        "internal health metrics."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "check_remote": {"type": "boolean", "description": "Also check Aka/Yanagiba heartbeats"},
        },
    },
    role_required=ROLE_ADMIN,
    tier=1,
)
async def handle_get_system_health(args: dict, ctx) -> str:
    from watchman import get_watchman
    from supervisor import get_supervisor
    try:
        wm = get_watchman()
        sv = get_supervisor()
        local_errors = await wm.get_recent_errors(hours=1)
        remote_health = "Not checked"
        if args.get("check_remote"):
            remote_health = await wm.get_remote_health()
        containers = await wm._docker_request("GET", "/containers/json", {"all": "1"})
        container_list = []
        if containers:
            for c in containers:
                container_list.append({
                    "name": c["Names"][0].lstrip("/"),
                    "status": c["Status"],
                    "state": c["State"],
                    "image": c["Image"],
                })
        health_data = {
            "supervisor_status": sv.get_status(),
            "local_containers": container_list,
            "remote_pihole_heartbeats": remote_health,
            "recent_local_errors": local_errors,
        }
        return json.dumps(health_data, indent=2)
    except Exception as e:
        return f"Failed to fetch system health: {e}"


@tool(
    name="get_container_logs",
    description="Fetch the recent logs for a specific Docker container on BernieHost.",
    input_schema={
        "type": "object",
        "properties": {
            "container_name": {"type": "string", "description": "Container name"},
            "hours":          {"type": "integer", "description": "Hours of history (default 1)"},
            "filter_errors":  {"type": "boolean", "description": "Only ERROR/Exception lines"},
        },
        "required": ["container_name"],
    },
    role_required=ROLE_ADMIN,
    tier=1,
)
async def handle_get_container_logs(args: dict, ctx) -> str:
    from watchman import get_watchman
    try:
        wm = get_watchman()
        name = args["container_name"]
        hours = args.get("hours", 1)
        filter_err = args.get("filter_errors", False)

        real_name = name
        containers = await wm._docker_request("GET", "/containers/json", {"all": "1"})
        if containers:
            for c in containers:
                c_name = c["Names"][0].lstrip("/")
                if c_name == name or name in c_name:
                    real_name = c_name
                    break

        since_ts = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
        raw = await wm._docker_request(
            "GET",
            f"/containers/{real_name}/logs",
            {"since": str(since_ts), "stderr": "1", "stdout": "1", "tail": "100"},
        )
        if not raw:
            return f"No logs found for container '{name}' in the last {hours}h."
        all_lines = wm._parse_docker_logs(raw)
        if filter_err:
            keywords = ["ERROR", "Exception", "Traceback", "failed"]
            log_lines = [l for l in all_lines if any(k in l for k in keywords)]
        else:
            log_lines = all_lines
        return json.dumps({
            "container": name,
            "hours_fetched": hours,
            "line_count": len(log_lines),
            "logs": log_lines[-100:],
        }, indent=2)
    except Exception as e:
        return f"Failed to fetch logs: {e}"


# ── Langfuse ────────────────────────────────────────────────────────────────
@tool(
    name="get_langfuse_traces",
    description=(
        "Fetch recent LLM traces (conversations, tool calls, latencies) "
        "from Langfuse."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit":   {"type": "integer", "description": "Traces to fetch (default 10, max 50)"},
            "user_id": {"type": "string",  "description": "Filter by person's name"},
        },
    },
    role_required=ROLE_BERNIE,
    tier=1,
)
async def handle_get_langfuse_traces(args: dict, ctx) -> str:
    lf_public = os.environ.get("LANGFUSE_PUBLIC_KEY")
    lf_secret = os.environ.get("LANGFUSE_SECRET_KEY")
    lf_host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    if not lf_public or not lf_secret or not lf_host:
        return "Langfuse keys not configured."

    limit = args.get("limit", 10)
    user_id = args.get("user_id")
    url = f"{lf_host}/api/public/traces?limit={limit}"
    if user_id:
        url += f"&userId={user_id}"
    auth_str = base64.b64encode(f"{lf_public}:{lf_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {auth_str}"}

    session = ctx.services.session
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status != 200:
                return f"Langfuse API error: {resp.status} - {await resp.text()}"
            data = await resp.json()
            traces = data.get("data", [])
            if not traces:
                return "No traces found."
            lines = []
            for t in traces:
                cost = t.get("totalCost", 0)
                lat = t.get("latency", 0)
                lines.append(
                    f"• Trace {t['id']} | User: {t.get('userId', '?')} | "
                    f"Cost: ${cost:.4f} | Latency: {lat:.1f}s | "
                    f"Input: {str(t.get('input'))[:100]}"
                )
            return "\n".join(lines)
    except Exception as e:
        return f"Failed to fetch traces: {e}"


@tool(
    name="get_langfuse_metrics",
    description=(
        "Fetch daily LLM usage metrics (token counts, cost, trace counts) "
        "from Langfuse."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Days back to report (default 7)"},
        },
    },
    role_required=ROLE_BERNIE,
    tier=1,
)
async def handle_get_langfuse_metrics(args: dict, ctx) -> str:
    lf_public = os.environ.get("LANGFUSE_PUBLIC_KEY")
    lf_secret = os.environ.get("LANGFUSE_SECRET_KEY")
    lf_host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    if not lf_public or not lf_secret or not lf_host:
        return "Langfuse keys not configured."

    days = args.get("days", 7)
    auth_str = base64.b64encode(f"{lf_public}:{lf_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {auth_str}"}
    url = f"{lf_host}/api/public/metrics/daily"

    session = ctx.services.session
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status != 200:
                return f"Langfuse API error: {resp.status} - {await resp.text()}"
            data = await resp.json()
            records = data.get("data", [])
            if not records:
                return "No metrics found."
            lines = []
            for r in records[:days]:
                lines.append(
                    f"• {r['date']}: {r['countTraces']} traces, "
                    f"${r.get('totalCost', 0):.4f}"
                )
                models = []
                for u in r.get("usage", []):
                    m = u.get("model", "unknown")
                    models.append(f"{m} ({u['totalUsage']}t)")
                if models:
                    lines.append(f"  Models: {', '.join(models)}")
            return "\n".join(lines)
    except Exception as e:
        return f"Failed to fetch metrics: {e}"


# ── Frigate camera toggle (slash parity) ──────────────────────────────────────
@tool(
    name="frigate_set_camera",
    description="Enable or disable Frigate person alerts for one specific camera. Use the camera id from the config (e.g. 'driveway', 'back_yard'). Admin only.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "camera": {"type": "string", "description": "Camera identifier (from config.frigate.cameras)"},
            "enabled": {"type": "boolean", "description": "true = turn alerts on, false = turn off"},
        },
        "required": ["camera", "enabled"],
    },
    role_required=ROLE_ADMIN,
    tier=3,
)
async def handle_frigate_set_camera(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have set frigate camera {args.get('camera')} to {args.get('enabled')}]"

    cam = args["camera"]
    enabled = bool(args["enabled"])

    from config import update_config
    await update_config({"frigate": {"cameras_enabled": {cam: enabled}}})

    frigate_cfg = ctx.config.get("frigate", {})
    label = frigate_cfg.get("cameras", {}).get(cam, cam)
    state = "enabled" if enabled else "disabled"
    return f"Frigate alerts for camera '{label}' ({cam}) are now {state}."


# ── Frigate hours change (slash parity) ────────────────────────────────────────
@tool(
    name="frigate_set_hours",
    description="Set active night hours for Frigate alerts. Format must be HH:MM (24-hour). Admin only.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "Start time in HH:MM format (24-hour)"},
            "end": {"type": "string", "description": "End time in HH:MM format (24-hour)"},
        },
        "required": ["start", "end"],
    },
    role_required=ROLE_ADMIN,
    tier=3,
)
async def handle_frigate_set_hours(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have set frigate night hours from {args.get('start')} to {args.get('end')}]"

    start = args["start"]
    end = args["end"]

    import re
    time_pat = r"^([01]\d|2[0-3]):[0-5]\d$"
    if not re.match(time_pat, start) or not re.match(time_pat, end):
        return "Error: Invalid time format. Both start and end must be in 24-hour HH:MM format (e.g. '22:00')."

    from config import update_config
    await update_config({"frigate": {"night_hours": {"start": start, "end": end}}})

    return f"Frigate active night hours have been set to start at {start} and end at {end}."


# ── Frigate mode (slash parity) ───────────────────────────────────────────────
@tool(
    name="frigate_set_mode",
    description="Set Frigate alert mode: on (standard alerts), off (silenced), or test (bypass night/quiet suppression). Matches /frigate_mode slash. Admin only.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "mode": {"type": "string", "description": "on, off, or test", "enum": ["on", "off", "test"]},
        },
        "required": ["mode"],
    },
    role_required=ROLE_ADMIN,
    tier=2,
)
async def handle_frigate_set_mode(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have set frigate mode to {args.get('mode')}]"

    mode = args["mode"].lower()
    if mode not in ("on", "off", "test"):
        return "Error: mode must be 'on', 'off', or 'test'."

    from config import update_config
    await update_config({"frigate": {"mode": mode, "test_mode": None}})

    return f"Frigate mode is now '{mode}'."


# ── list_slash_commands (parity + introspection) ──────────────────────────────
@tool(
    name="list_slash_commands",
    description="Returns the complete list of Discord slash commands (name + description) that users can invoke. For agent runtime introspection and NL parity. Excludes the exempt /shadow_mode.",
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ALL,
    domain="notify",
    tier=1,
)
async def handle_list_slash_commands(args: dict, ctx) -> str:
    if ctx.shadow:
        return "[shadow: would list slash commands]"

    # Use authoritative source (AST-extracted from bot.py + transit_discord.py).
    # This guarantees the list in the tool matches the actual registered slash commands
    # without hand-duplicating or modifying the @tree.command sites.
    commands = get_all_slash_commands()
    lines = [f"/{c['name']} — {c['description']}" for c in commands]
    return "Available Discord slash commands (use list_slash_commands for runtime query):\n" + "\n".join(lines)


# ── Admin config parity (config_summary, config_reminders) ────────────────────
@tool(
    name="set_config_summary",
    description="Admin: set the daily summary / highlights post time (config_summary slash parity). hour 0-23, minute 0-59. Persists via config.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "hour": {"type": "integer", "description": "0-23"},
            "minute": {"type": "integer", "description": "0-59"},
        },
        "required": ["hour", "minute"],
    },
    role_required=ROLE_ADMIN,
    tier=2,
)
async def handle_set_config_summary(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would set config_summary {args}]"
    h = int(args["hour"])
    m = int(args["minute"])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return "Invalid time: hour 0-23, minute 0-59."
    from config import update_config
    try:
        from background_scheduler import get_scheduler, timing_snapshot

        prior = timing_snapshot(ctx.config)
    except Exception:
        prior = None
    await update_config({"summary_hour": h, "summary_minute": m})
    if prior and (prior.get("summary_hour") != h or prior.get("summary_minute") != m):
        try:
            get_scheduler().sync_intervals_from_config(prior)
        except Exception:
            pass  # config persisted; reschedule when BTS is up
    return f"Daily summary time set to {h:02d}:{m:02d}."

@tool(
    name="set_config_reminders",
    description="Admin: set default reminder lead time in minutes (config_reminders slash parity).",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "minutes": {"type": "integer", "description": "Minutes before event, e.g. 15"},
        },
        "required": ["minutes"],
    },
    role_required=ROLE_ADMIN,
    tier=2,
)
async def handle_set_config_reminders(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would set config_reminders {args}]"
    mins = int(args["minutes"])
    if mins < 0:
        return "Minutes must be >= 0."
    from config import update_config
    await update_config({"default_reminder_minutes": [mins]})
    return f"Default reminder lead time set to {mins} minutes."


_EVAL_CAPTURE_OFF_NOTE = (
    "Also sets legacy eval.enabled=false. Nightly scoring turns off via that "
    "fallback unless eval.nightly.enabled was set explicitly (/nightly_eval)."
)
_EXPLICIT_NESTED_NOTE = (
    "/eval_mode on does not override an explicit nested false "
    "(use this command again to re-enable)."
)
@tool(
    name="set_eval_mode",
    description="[Admin] Enable or disable the shadow evaluation pipeline capture (eval_mode slash parity).",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean", "description": "true to enable, false to disable"},
        },
        "required": ["enabled"],
    },
    role_required=ROLE_ADMIN,
    tier=2,
)
async def handle_set_eval_mode(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would set eval enabled={args.get('enabled')}]"
    enabled = bool(args["enabled"])
    from config import update_config
    await update_config({"eval": {"capture": {"enabled": enabled}, "enabled": enabled}})
    state = "enabled" if enabled else "disabled"
    note = f" {_EVAL_CAPTURE_OFF_NOTE}" if not enabled else ""
    return f"Shadow eval capture {state}.{note}"

@tool(
    name="set_nightly_eval_mode",
    description="[Admin] Enable or disable nightly scoring for shadow eval.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
        },
        "required": ["enabled"],
    },
    role_required=ROLE_ADMIN,
    tier=2,
)
async def handle_set_nightly_eval_mode(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would set nightly eval enabled={args.get('enabled')}]"
    enabled = bool(args["enabled"])
    from config import update_config
    await update_config({"eval": {"nightly": {"enabled": enabled}}})
    return (
        f"Nightly eval scoring {'enabled' if enabled else 'disabled'}. "
        f"Sets eval.nightly.enabled explicitly; {_EXPLICIT_NESTED_NOTE}"
    )

@tool(
    name="set_harness_mode",
    description="[Admin] Enable or disable the harness (triplet) shadow eval leg.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
        },
        "required": ["enabled"],
    },
    role_required=ROLE_ADMIN,
    tier=2,
)
async def handle_set_harness_mode(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would set harness enabled={args.get('enabled')}]"
    enabled = bool(args["enabled"])
    from config import update_config
    await update_config({"eval": {"harness": {"enabled": enabled}}})
    return (
        f"Shadow harness {'enabled' if enabled else 'disabled'}. "
        f"Sets eval.harness.enabled explicitly; {_EXPLICIT_NESTED_NOTE}"
    )

@tool(
    name="set_eval_scoring",
    description="[Admin] Toggle pair/triplet scoring (both, pair, triplet, or none).",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["both", "pair", "triplet", "none"]},
        },
        "required": ["mode"],
    },
    role_required=ROLE_ADMIN,
    tier=2,
)
async def handle_set_eval_scoring(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would set eval scoring mode={args.get('mode')}]"
    mode = args["mode"]
    if mode == "none":
        score_pairs = False
        score_triplets = False
    else:
        score_pairs = mode in ("both", "pair")
        score_triplets = mode in ("both", "triplet")
    from config import update_config
    await update_config({"eval": {"nightly": {"score_pairs": score_pairs, "score_triplets": score_triplets}}})
    return f"Eval scoring set to pairs={score_pairs}, triplets={score_triplets}."

@tool(
    name="set_hitl_mode",
    description="[Admin] Enable or disable HITL (Human-in-the-loop) DMs for shadow eval.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
        },
        "required": ["enabled"],
    },
    role_required=ROLE_ADMIN,
    tier=2,
)
async def handle_set_hitl_mode(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would set hitl mode={args.get('enabled')}]"
    enabled = bool(args["enabled"])
    from config import update_config
    await update_config({"eval": {"nightly": {"hitl": enabled}}})
    return f"HITL DMs {'enabled' if enabled else 'disabled'}."

@tool(
    name="set_worker_model",
    description="[Admin] Set the model used for background/cognitive workers (worker_model slash parity).",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "Model name (from configured models or 'ollama_xxx')"},
        },
        "required": ["model"],
    },
    role_required=ROLE_ADMIN,
    tier=2,
)
async def handle_set_worker_model(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would set worker_model {args}]"
    model = args["model"].strip()
    if not model:
        return "Model name required."
    from config import update_config
    await update_config({"eval": {"worker_model": model}})
    return f"Worker model set to `{model}`."

@tool(
    name="get_eval_status",
    description="[Admin] Return current shadow eval status, enabled flag, models, daily counts, unscored (eval_status slash parity). Uses public DB helpers.",
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ADMIN,
    tier=1,
)
async def handle_get_eval_status(args: dict, ctx) -> str:
    if ctx.shadow:
        return "[shadow: would return eval status]"
    from datetime import datetime
    from config import TASK_TZ
    from db_binding import get_database

    db = ctx.services.db if ctx.services and ctx.services.db else get_database()
    from eval.policy import resolve_eval_policy, harness_active
    policy = resolve_eval_policy(ctx.config or {})
    
    worker_model = (ctx.config or {}).get("eval", {}).get("worker_model") or "default"
    eval_model = policy.eval_model or "default"
    daily_cap = policy.shadow_daily_cap
    executor_cfg = (ctx.config or {}).get("executor", {})
    legacy_harness = executor_cfg.get("shadow_harness_enabled")
    legacy_defer = executor_cfg.get("shadow_defer_s")
    today_str = datetime.now(TASK_TZ).strftime("%Y-%m-%d")
    try:
        calls_today = await db.get_shadow_call_count_today(today_str)
    except Exception:
        calls_today = -1
    try:
        unscored = await db.get_unscored_shadow_calls(today_str)
        unscored_count = len(unscored)
    except Exception:
        unscored_count = -1
        
    return (
        f"Shadow Eval Status\n"
        f"Capture Enabled: {'✅ yes' if policy.capture_enabled else '⏸️ no'}\n"
        f"Harness Enabled: {'✅ yes' if policy.harness_enabled else '⏸️ no'} (Active right now: {harness_active(policy)})\n"
        f"Nightly Enabled: {'✅ yes' if policy.nightly_enabled else '⏸️ no'}\n"
        f"Score pairs: {'✅ yes' if policy.score_pairs else '⏸️ no'}\n"
        f"Score triplets: {'✅ yes' if policy.score_triplets else '⏸️ no'}\n"
        f"HITL DMs: {'✅ yes' if policy.hitl else '⏸️ no'}\n"
        f"Ungrounded audit: {'✅ yes' if policy.ungrounded_audit else '⏸️ no'}\n"
        f"Shadow model: `{policy.shadow_model or 'none'}`\n"
        f"Worker model: `{worker_model}`\n"
        f"Judge/eval model: `{eval_model}`\n"
        f"Legacy executor.shadow_harness_enabled: `{legacy_harness}`\n"
        f"Legacy executor.shadow_defer_s: `{legacy_defer}`\n"
        f"Daily cap: {calls_today}/{daily_cap}\n"
        f"Unscored (before today): {unscored_count}"
    )
