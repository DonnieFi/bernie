"""Home Assistant tool handlers (read + control).

Domain handler; dispatched via ToolGateway / llm.compat.execute_tool.
Per-tool RBAC mirrors the original `require_permission()` checks against
config 'permissions' map.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from tools import ROLE_ALL, tool

log = logging.getLogger(__name__)


def _check(ctx, perm_name: str) -> tuple[bool, str]:
    """Wrap require_permission with current ToolContext."""
    from tool_utils import require_permission
    return require_permission(ctx.group, perm_name, ctx.config)


def _ha(ctx):
    """Prefer the injected HA service; fall back to the module-level singleton."""
    svc = getattr(ctx.services, "ha", None)
    if svc is not None:
        return svc
    from ha_service import ha_service
    return ha_service


def _network(ctx):
    """Prefer the injected network service; fall back to module-level singleton."""
    svc = getattr(ctx.services, "network", None)
    if svc is not None:
        return svc
    from network_service import network_service
    return network_service


# ── control_device (WRITE) ──────────────────────────────────────────────────
@tool(
    name="control_device",
    description="Turn a Home Assistant device on, off, or toggle it. Requires the HA entity_id — use get_home_state to look one up if unknown. Confirm the action after it completes; never pre-confirm.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "description": "The ID of the device (e.g., 'light.living_room')"},
            "action":    {"type": "string", "description": "'turn_on', 'turn_off', or 'toggle'"},
        },
        "required": ["entity_id", "action"],
    },
    role_required=ROLE_ALL,
    tier=3,
)
async def handle_control_device(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called control_device({args})]"
    allowed, msg = _check(ctx, "common_areas")
    if not allowed:
        return msg

    ha_service = _ha(ctx)

    entity_id = args["entity_id"]
    action = args["action"]
    if "." not in entity_id:
        resolved = ha_service.resolve_entity_id(entity_id)
        if resolved:
            log.info("Resolved '%s' → '%s'", entity_id, resolved)
            entity_id = resolved
        else:
            return (
                f"Could not find a device matching '{entity_id}'. "
                "Try using the exact entity ID (e.g. 'light.living_room')."
            )

    success = False
    if action == "turn_on":
        success = await ha_service.turn_on(entity_id)
    elif action == "turn_off":
        success = await ha_service.turn_off(entity_id)
    elif action == "toggle":
        success = await ha_service.toggle(entity_id)

    if success:
        return f"Successfully executed '{action}' on {entity_id}."
    return f"Failed to execute '{action}' on {entity_id}. Check entity ID and HA connection."


# ── set_light (WRITE) ───────────────────────────────────────────────────────
@tool(
    name="set_light",
    description="Set brightness, colour temperature, or RGB on a light.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "entity_id":         {"type": "string", "description": "Light entity_id or friendly name"},
            "brightness_pct":    {"type": "integer", "description": "Brightness percent 0–100 (0 turns light off)"},
            "color_temp_kelvin": {"type": "integer", "description": "Optional colour temp in K (e.g. 2700, 4000, 6500)"},
            "rgb_color":         {"type": "array",  "items": {"type": "integer"},
                                   "description": "Optional [R,G,B] colour 0–255"},
        },
        "required": ["entity_id"],
    },
    role_required=ROLE_ALL,
    tier=2,
)
async def handle_set_light(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called set_light({args})]"
    allowed, msg = _check(ctx, "own_room")
    if not allowed:
        return msg
    ha_service = _ha(ctx)

    entity_id = args["entity_id"]
    if "." not in entity_id:
        resolved = ha_service.resolve_entity_id(entity_id)
        if resolved:
            entity_id = resolved
        else:
            return f"Could not find a light matching '{entity_id}'."
    brightness_pct = args.get("brightness_pct")
    if brightness_pct == 0:
        success = await ha_service.turn_off(entity_id)
    else:
        brightness = round(brightness_pct / 100 * 255) if brightness_pct is not None else None
        color_temp = args.get("color_temp_kelvin") or None
        rgb = args.get("rgb_color")
        if not rgb or all(v == 0 for v in rgb):
            rgb = None
        success = await ha_service.set_light_state(
            entity_id, on=True, brightness=brightness, color_temp=color_temp, rgb=rgb,
        )
    if success:
        parts = []
        if brightness_pct is not None:
            parts.append(f"brightness {brightness_pct}%")
        if args.get("color_temp_kelvin"):
            parts.append(f"colour temp {args['color_temp_kelvin']}K")
        if args.get("rgb_color"):
            parts.append(f"colour {args['rgb_color']}")
        desc = ", ".join(parts) or "settings"
        return f"Set {entity_id} to {desc}."
    return f"Failed to update {entity_id}. Check entity ID and HA connection."


# ── trigger_automation (WRITE) ──────────────────────────────────────────────
@tool(
    name="trigger_automation",
    description="Trigger a Home Assistant automation by entity_id. If you don't have the ID, call get_home_state with domain='automation' first.",
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "automation_id": {"type": "string", "description": "HA automation entity_id"},
        },
        "required": ["automation_id"],
    },
    role_required=ROLE_ALL,
    tier=2,
)
async def handle_trigger_automation(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called trigger_automation({args})]"
    allowed, msg = _check(ctx, "whole_home")
    if not allowed:
        return msg
    ha_service = _ha(ctx)

    auto_id = args["automation_id"]
    success = await ha_service.trigger_automation(auto_id)
    if success:
        return f"Successfully triggered {auto_id}."
    return f"Failed to trigger {auto_id}. Check automation ID and HA connection."


# ── get_home_state (READ) ───────────────────────────────────────────────────
@tool(
    name="get_home_state",
    description=(
        "Get the state of home devices and sensors. Pass entity_id for a specific device, "
        "domain to list all entities in a domain (e.g. 'sensor', 'light', 'switch'), or "
        "query to search ALL entities by keyword (e.g. 'garmin', 'sonos', 'nirochan', 'temp'). "
        "Discovery before deflection: try query= before telling the user a device doesn't exist."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "description": "Specific entity ID to query"},
            "domain":    {"type": "string", "description": "HA domain to list (e.g. 'sensor', 'light')"},
            "query":     {"type": "string", "description": "Keyword to search across entities"},
        },
        "required": [],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_home_state(args: dict, ctx) -> str:
    from tool_utils import compact_tool_result

    ha_service = _ha(ctx)

    entity_id = args.get("entity_id")
    domain = args.get("domain")
    query = args.get("query")

    if entity_id:
        state = await ha_service.get_state(entity_id)
        if not state:
            return f"Could not fetch state for {entity_id}."
        attrs = state.get("attributes", {})
        friendly = attrs.get("friendly_name", entity_id)
        return f"{friendly} ({entity_id}) is {state.get('state')}. Attributes: {attrs}"

    if query:
        q = query.lower()
        all_states = await ha_service.get_live_states()
        matches = [
            s
            for s in all_states
            if q in s.get("entity_id", "").lower()
            or q in s.get("attributes", {}).get("friendly_name", "").lower()
        ]
        if not matches:
            return f"No entities found matching '{query}'."
        lines = [
            f"• {s.get('attributes', {}).get('friendly_name', s.get('entity_id'))} "
            f"({s.get('entity_id')}) → {s.get('state')}"
            for s in matches
        ]
        raw = f"{len(lines)} entities matching '{query}':\n" + "\n".join(lines)
        return compact_tool_result("get_home_state", raw)

    if domain:
        states = await ha_service.get_live_states(domain=domain)
        if not states:
            return f"No entities found for domain '{domain}'."
        lines = [
            f"• {s.get('attributes', {}).get('friendly_name', s.get('entity_id'))} "
            f"({s.get('entity_id')}) → {s.get('state')}"
            for s in states
        ]
        raw = f"{len(lines)} {domain} entities:\n" + "\n".join(lines)
        return compact_tool_result("get_home_state", raw)

    states = await ha_service.get_all_states()
    if not states:
        return "No smart home devices found or HA is unavailable."
    lines = [f"• {s.get('entity_id')} is {s.get('state')}" for s in states]
    return compact_tool_result("get_home_state", "\n".join(lines))


# ── get_home_health (READ) ──────────────────────────────────────────────────
@tool(
    name="get_home_health",
    description="Report Home Assistant devices that haven't reported recently.",
    input_schema={
        "type": "object",
        "properties": {
            "stale_minutes": {"type": "integer", "description": "Threshold for staleness, default 60"},
        },
        "required": [],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_home_health(args: dict, ctx) -> str:
    db = ctx.services.db
    ha_service = _ha(ctx)

    stale_min = args.get("stale_minutes", 60)
    db_stale = await db.get_stale_ha_devices(stale_min)
    live_states = await ha_service.get_live_states()
    live_lookup = {s["entity_id"]: s for s in live_states}

    final_stale = []
    now_ts = time.time()
    for d in db_stale:
        eid = d["entity_id"]
        if eid in live_lookup:
            lu = live_lookup[eid].get("last_updated")
            if lu:
                try:
                    age = now_ts - datetime.fromisoformat(lu).timestamp()
                    if age < (stale_min * 60):
                        continue  # Device is actually fresh in memory
                except Exception:
                    pass
        final_stale.append(d)

    if not final_stale:
        return f"All tracked devices reported within the last {stale_min} minutes."
    lines = [
        f"- {d['name']} ({d['entity_id']}): last seen {d['last_updated'] or 'unknown'}"
        for d in final_stale
    ]
    return f"{len(final_stale)} device(s) stale (>{stale_min} min):\n" + "\n".join(lines)


# ── get_network_devices (READ) ──────────────────────────────────────────────
@tool(
    name="get_network_devices",
    description=(
        "List all devices currently or recently seen on the home network "
        "(Unifi AND Google WiFi). Returns names, IPs, MAC addresses, "
        "connection status, and real-time bandwidth throughput."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "filter":      {"type": "string",  "description": "Optional search filter (name, IP, or MAC)"},
            "online_only": {"type": "boolean", "description": "If true, only return devices currently online"},
        },
        "required": [],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_network_devices(args: dict, ctx) -> str:
    network_service = _network(ctx)
    try:
        devices = await network_service.get_devices()
        q = args.get("filter", "").lower()
        online_only = args.get("online_only", False)
        if online_only:
            devices = [d for d in devices if d.get("seen") == "now"]
        if q:
            devices = [
                d
                for d in devices
                if q in d["mac"]
                or q in d["display_name"].lower()
                or q in d["vendor"].lower()
                or q in d["ip"]
                or q in d["hostname"].lower()
            ]
        if not devices:
            return "No matching network devices found."
        lines = [f"Found {len(devices)} device(s):"]
        for d in devices[:40]:
            name = d["display_name"] or "Unnamed"
            status = f"[{d['seen']}]" if d["seen"] else "[offline]"
            bw = (
                f" · ↓{d.get('rx_mbps', 0.0)} Mbit/s ↑{d.get('tx_mbps', 0.0)} Mbit/s"
                if d.get("rx_mbps") or d.get("tx_mbps")
                else ""
            )
            lines.append(f"• {name} · {d['ip']} · {d['vendor']} {status} ({d['network']}){bw}")
        if len(devices) > 40:
            lines.append(f"...and {len(devices)-40} more.")
        return "\n".join(lines)
    except Exception as e:
        log.error("Error in get_network_devices: %s", e)
        return "Failed to fetch network device data."


# ── get_garbage_schedule ────────────────────────────────────────────────────
@tool(
    name="get_garbage_schedule",
    description=(
        "Get the upcoming garbage, organics (green bin), and recycling "
        "collection schedule."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_garbage_schedule(args: dict, ctx) -> str:
    if ctx.services.session is None:
        return "Garbage schedule unavailable right now."
    from garbage_service import get_next_collections
    from zoneinfo import ZoneInfo
    tz_local = ZoneInfo(ctx.config.get("timezone", "America/Halifax"))
    events = await get_next_collections(
        ctx.config.get("recollect_ics_url"),
        tz_local,
        ctx.services.session,
        days=14,
    )
    if not events:
        return "No upcoming garbage/recycling collections found."
    lines = ["Upcoming collections:"]
    for e in events:
        date_str = e["date"].strftime("%A, %b %d")
        lines.append(f"- {date_str}: {e['summary']}")
    return "\n".join(lines)


# ── get_oura_sleep ──────────────────────────────────────────────────────────
@tool(
    name="get_oura_sleep",
    description=(
        "Get Oura ring sleep data for a given date (defaults to last night). "
        "Returns sleep score, efficiency, stage durations (REM/deep/light), "
        "HRV, and heart rate. Use alongside Garmin data when comparing sleep "
        "trackers or when Oura-specific metrics are requested."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Date to fetch in YYYY-MM-DD format. Omit for last night.",
            },
            "date_offset": {
                "type": "integer",
                "description": "Optional relative offset in days from today (e.g. -1 for yesterday, -2 for the day before). Evaluated if date is not provided.",
            }
        },
        "required": [],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_oura_sleep(args: dict, ctx) -> str:
    from oura_service import get_sleep as _oura_sleep
    import json as _json
    from datetime import date as _date, timedelta as _timedelta
    
    target = None
    raw_date = args.get("date")
    offset = args.get("date_offset")
    
    if raw_date:
        target = _date.fromisoformat(raw_date)
    elif offset is not None:
        target = _date.today() + _timedelta(days=offset)
        
    result = await _oura_sleep(target)
    if result is None:
        return "Oura: could not fetch sleep data — check OURA_TOKEN."
    if result.get("no_data"):
        return f"Oura: no sleep session recorded for {result['date']}."
    return _json.dumps({k: v for k, v in result.items() if v is not None}, indent=2)


# ── Dedicated for /temps /ha_entities slash parity via get_home_state coverage ─
@tool(
    name="get_temperatures",
    description="Current temperatures from home sensors (equivalent observable to /temps). Delegates to home state query.",
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_temperatures(args: dict, ctx) -> str:
    # Use ha_service directly (matches /temps slash: sensors + 24h history min/max); no direct handler call
    ha = _ha(ctx)
    try:
        sensors = await ha.get_temperature_sensors()
        if not sensors:
            # fallback without calling other handler
            all_states = await ha.get_live_states()
            temps = [
                f"{s.get('attributes',{}).get('friendly_name', s.get('entity_id'))}: {s.get('state')}"
                for s in all_states
                if 'temp' in (s.get('entity_id','') + str(s.get('attributes',{}).get('friendly_name',''))).lower()
            ]
            if not temps:
                return "No temperature sensors found. Try get_home_state(query='temp')."
            return "Temperatures:\n" + "\n".join(temps)
        lines = []
        for s in sensors:
            eid = s.get("entity_id", "")
            name = s.get("attributes", {}).get("friendly_name", eid)
            current = s.get("state", "?")
            unit = s.get("attributes", {}).get("unit_of_measurement", "°C")
            try:
                history = await ha.get_temperature_history(eid, hours=24)
                temps = []
                for h in history:
                    try:
                        temps.append(float(h.get("state", "")))
                    except (ValueError, TypeError):
                        pass
                if temps:
                    val = f"{current}{unit}  (↑ {max(temps):.1f} / ↓ {min(temps):.1f})"
                else:
                    val = f"{current}{unit}"
            except Exception:
                val = f"{current}{unit}"
            lines.append(f"• {name}: {val}")
        return "🌡️ Home Temperatures\n" + "\n".join(lines)
    except Exception as e:
        return f"Temp fetch error: {e}"

@tool(
    name="list_ha_entities",
    description="List or search HA entities (equivalent to /ha_entities). Use query param or falls back to domain.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Optional keyword filter"},
            "domain": {"type": "string", "description": "Optional domain e.g. sensor"},
        },
        "required": [],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_list_ha_entities(args: dict, ctx) -> str:
    # Use service directly; default to all entities (matches /ha_entities); for filter, apply same logic as get_home_state but without calling the handler fn
    q = (args.get("query") or "").strip().lower()
    dom = (args.get("domain") or "").strip()
    ha = _ha(ctx)
    try:
        if dom:
            states = await ha.get_live_states(domain=dom)
        else:
            states = await ha.get_live_states()
        if q:
            states = [s for s in states if q in s.get("entity_id","").lower() or q in str(s.get("attributes",{}).get("friendly_name","")).lower()]
        if not states:
            return "No Home Assistant devices found or Home Assistant is currently unreachable."
        lines = [f"• {s.get('attributes',{}).get('friendly_name', s.get('entity_id'))} ({s.get('entity_id')}) → {s.get('state')}" for s in states]
        return "\n".join(lines)
    except Exception as e:
        return f"HA list error: {e}"


@tool(
    name="ha_assist",
    description=(
        "Send natural language to Home Assistant Assist / Conversation API "
        "(e.g. 'turn off the kitchen lights', 'what is the living room temperature'). "
        "Use when the user speaks in area/device language without a known entity_id."
    ),
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Natural language command or question for HA Assist",
            },
            "language": {
                "type": "string",
                "description": "Language code (default en)",
            },
        },
        "required": ["text"],
    },
    role_required=ROLE_ALL,
    tier=2,
    domain="home",
)
async def handle_ha_assist(args: dict, ctx) -> str:
    """family-bot-5hy.13"""
    if ctx.shadow:
        return f"[shadow: would have called ha_assist({args.get('text')!r})]"
    text = (args.get("text") or "").strip()
    if not text:
        return "ha_assist requires non-empty text."
    language = (args.get("language") or "en").strip() or "en"
    ha = _ha(ctx)
    out = await ha.conversation_process(text, language=language)
    if not out.get("ok"):
        err = out.get("error") or out.get("status") or "unknown error"
        return f"HA Assist failed: {err}"
    result = out.get("result") or {}
    # HA response shapes vary by version
    speech = None
    resp = result.get("response") if isinstance(result, dict) else None
    if isinstance(resp, dict):
        sp = resp.get("speech")
        if isinstance(sp, dict):
            plain = sp.get("plain") if isinstance(sp.get("plain"), dict) else {}
            speech = plain.get("speech") or sp.get("speech")
        elif isinstance(sp, str):
            speech = sp
    if not speech and isinstance(result.get("speech"), dict):
        plain = result["speech"].get("plain") or {}
        if isinstance(plain, dict):
            speech = plain.get("speech")
    if not speech:
        import json
        speech = json.dumps(result, default=str)[:1500]
    return f"HA Assist: {speech}"


@tool(
    name="inspect_device",
    description=(
        "Unified device diagnostic: resolve by MAC, HA entity_id, or friendly name "
        "and merge network + HA (+ optional Frigate camera hints) into one summary."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "MAC (aa:bb:…), entity_id (light.x), or name fragment",
            },
        },
        "required": ["query"],
    },
    role_required=ROLE_ALL,
    tier=1,
    domain="home",
)
async def handle_inspect_device(args: dict, ctx) -> str:
    """family-bot-5hy.14"""
    q = (args.get("query") or "").strip()
    if not q:
        return "inspect_device requires a query (mac, entity_id, or name)."

    lines: list[str] = [f"## Device inspect: {q}"]
    ha = _ha(ctx)
    net = _network(ctx)

    # Network match
    mac_q = q.lower().replace("-", ":")
    try:
        devices = await net.get_devices()
    except Exception as e:
        devices = []
        lines.append(f"Network list error: {e}")
    net_hits = []
    for d in devices or []:
        mac = (d.get("mac") or "").lower()
        name = " ".join(
            str(d.get(k) or "") for k in ("display_name", "unifi_name", "hostname", "name")
        ).lower()
        if mac_q == mac or mac_q in mac or q.lower() in name:
            net_hits.append(d)
    if net_hits:
        lines.append("### Network")
        for d in net_hits[:5]:
            lines.append(
                f"• {d.get('display_name') or d.get('unifi_name') or d.get('hostname') or d.get('mac')} "
                f"| mac={d.get('mac')} ip={d.get('ip') or '—'} "
                f"active={d.get('is_active')} net={d.get('network')} "
                f"essid={d.get('essid') or '—'} "
                f"rx/tx={d.get('rx_mbps', '—')}/{d.get('tx_mbps', '—')} Mbps"
            )
    else:
        lines.append("### Network\n• No matching client in UniFi/HA scanner map.")

    # HA match
    eid = None
    if "." in q and " " not in q:
        eid = q
    else:
        eid = ha.resolve_entity_id(q)
    if eid:
        try:
            st = await ha.get_state(eid)
        except Exception as e:
            st = {}
            lines.append(f"HA state error: {e}")
        if st:
            attrs = st.get("attributes") or {}
            lines.append("### Home Assistant")
            lines.append(
                f"• {attrs.get('friendly_name', eid)} ({eid}) → {st.get('state')}"
            )
            extra = []
            for k in (
                "device_class",
                "unit_of_measurement",
                "brightness",
                "current_temperature",
                "battery_level",
                "source",
            ):
                if k in attrs and attrs[k] is not None:
                    extra.append(f"{k}={attrs[k]}")
            if extra:
                lines.append("  " + ", ".join(extra[:8]))
            if st.get("last_updated"):
                lines.append(f"  last_updated={st.get('last_updated')}")
    else:
        lines.append("### Home Assistant\n• No matching entity.")

    # Frigate camera name hint
    try:
        from config import config
        cams = (config.get("frigate") or {}).get("cameras") or {}
        cam_hits = [
            f"{cid} ({label})"
            for cid, label in cams.items()
            if q.lower() in str(cid).lower() or q.lower() in str(label).lower()
        ]
        if cam_hits:
            lines.append("### Frigate cameras\n• " + ", ".join(cam_hits[:8]))
    except Exception:
        pass

    return "\n".join(lines)

