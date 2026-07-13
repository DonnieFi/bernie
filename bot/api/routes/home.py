"""API routes: home (family-bot-8lx.2 hard-cut)."""
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


def build_home_router(ctx: Any) -> APIRouter:
    """Register home routes; closes over container services via ctx."""
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

    @router.get("/api/rooms", dependencies=[Depends(_ac.verify_token)])
    async def get_rooms():
        try:
            return await _fetch_rooms_data()
        except Exception as e:
            log.error(f"Rooms error: {e}"); raise HTTPException(status_code=500, detail=str(e))

    def _invalidate_home_caches(*extra_keys: str) -> None:
        """Drop dashboard + domain caches so next Home load sees live HA state."""
        for key in ("home_dashboard", "home_switches", "home_media", "home_automations", "home_system", *extra_keys):
            _ac._cache.pop(key, None)

    def _light_state_event(entity_id: str, is_on: bool) -> dict:
        """Shape must match ha_service._broadcast_light_state + web WS handler."""
        slug = entity_id.split(".")[-1].replace("_", "-")
        return {
            "type": "light.state",
            "id": slug,
            "on": is_on,
            "last": f"{'on' if is_on else 'off'} · just now",
        }

    @router.post("/api/lights/{id}", dependencies=[Depends(_ac.verify_token)])
    async def toggle_light(id: str, req: _ac.LightControl):
        entity_id = f"light.{id.replace('-', '_')}"
        success = await ha_service.set_light_state(
            entity_id, req.on,
            brightness=req.brightness,
            color_temp=req.color_temp,
            rgb=req.rgb
        )
        if success:
            state_str = "on" if req.on else "off"
            await db_writes.log_activity("light", f"Turned <b>{entity_id}</b> {state_str}", "Source: Web UI", "HA")
            _invalidate_home_caches()
            # ha_service may also broadcast; client is idempotent on light.state.
            await connection_manager.broadcast(_light_state_event(entity_id, req.on))
            return {"ok": True, "on": req.on}
        raise HTTPException(status_code=500, detail="Failed to control light")

    @router.get("/api/network/devices", dependencies=[Depends(_ac.verify_token)])
    async def get_network_devices():
        from network_service import network_service
        return await network_service.get_devices()

    @router.get("/api/cameras/{camera}/snapshot", dependencies=[Depends(_ac.verify_token)])
    async def get_camera_snapshot(camera: str, refresh: bool = False):
        result = await _frigate.get_snapshot(camera, use_cache=not refresh)
        if not result: raise HTTPException(status_code=404)
        return Response(content=result[0], media_type=result[1])

    @router.get("/api/cameras/config", dependencies=[Depends(_ac.verify_token)])
    async def get_camera_config():
        return _ac.config.get("frigate", {})

    @router.get("/api/cameras/events", dependencies=[Depends(_ac.verify_token)])
    async def get_camera_events(limit: int = 20, camera: str | None = None):
        events = await _frigate.get_events(limit=limit, camera=camera)
        if events is None: raise HTTPException(status_code=500, detail="Failed to fetch events")
        return events

    @router.get("/api/cameras/events/{event_id}/snapshot", dependencies=[Depends(_ac.verify_token)])
    async def get_camera_event_snapshot(event_id: str, crop: bool = True):
        result = await _frigate.get_event_snapshot(event_id, crop=crop)
        if not result: raise HTTPException(status_code=404)
        return Response(content=result[0], media_type=result[1])

    @router.post("/api/cameras/mode", dependencies=[Depends(_ac.verify_token)])
    async def set_camera_mode(payload: dict):
        from config import update_config
        mode = payload.get("mode")
        if mode not in ["on", "off", "test"]:
            raise HTTPException(status_code=400, detail="Invalid mode")
        await update_config({"frigate": {"mode": mode}})
        return {"status": "ok", "mode": mode}

    @router.post("/api/cameras/{camera}/enable", dependencies=[Depends(_ac.verify_token)])
    async def set_camera_enable(camera: str, payload: dict):
        from config import update_config
        enabled = payload.get("enabled")
        if enabled is None: raise HTTPException(status_code=400)
        await update_config({"frigate": {"cameras_enabled": {camera: bool(enabled)}}})
        return {"status": "ok", "camera": camera, "enabled": bool(enabled)}

    @router.get("/api/debug/config", dependencies=[Depends(_ac.verify_token)])
    async def debug_config():
        return {"keys": list(_ac.config.keys()), "family_members": list(_ac.config.get("family_members", {}).keys())}

    # --- Presence ---
    @router.get("/api/presence", dependencies=[Depends(_ac.verify_token)])
    async def get_presence():
        return await presence_service.get_full_presence()

    @router.post("/api/presence/webhook")
    async def presence_webhook(data: Dict[str, Any]):
        log.info(f"Received presence webhook: {data}")
        return {"status": "received"}

    @router.post("/api/presence/{person_id}/set", dependencies=[Depends(_ac.verify_token)])
    async def set_presence(person_id: str, data: Dict[str, Any]):
        is_home = data.get("home")
        if is_home is None:
            raise HTTPException(status_code=400, detail="Missing 'home' field")
        # person_id is canonical (matches presence_service / apply_presence_tick keys)
        await db_writes.update_presence(person_id, bool(is_home))
        return {"ok": True, "person_id": person_id, "home": bool(is_home)}

    # --- Ping ---
    @router.post("/api/ping/{who}", dependencies=[Depends(_ac.verify_token)])
    async def ping_family_member(who: str, req: _ac.PingRequest):
        person_id = person_registry.resolve(who)
        if not person_id:
            return {"ok": False, "reason": "person not found"}
        
        person = person_registry.get(person_id)
        discord_id = person.get("discord_id") if person else None
        
        if not discord_id:
            return {"ok": False, "reason": "no Discord account"}
        try:
            await notification_dispatcher.ping(str(discord_id), req.text)
            return {"ok": True, "to": who}
        except Exception as e:
            log.error(f"Error pinging {who}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # --- Lights out automation ---
    @router.post("/api/automations/lights-out", dependencies=[Depends(_ac.verify_token)])
    async def trigger_lights_out():
        success = await ha_service.trigger_automation("automation.lights_out")
        if success:
            log.info("Triggered automation.lights_out")
            await db_writes.log_activity("light", "Triggered <b>Lights Out</b>", "Source: Web UI", "HA")
            return {"ok": True}
        raise HTTPException(status_code=500, detail="Failed to trigger automation")

    # --- Memory ---
    @router.get("/api/memory/{person_id}", dependencies=[Depends(_ac.verify_token)])
    async def get_memory(person_id: str):
        from memory_service import get_patterns
        
        db_person_id = person_registry.resolve(person_id) or person_id
        try:
            patterns = await get_patterns(db_person_id)
            rows = await db.list_memory_events(db_person_id, limit=30)
            events = [{"id": r["id"], "type": r["event_type"], "title": r["title"], "logged_at": r["logged_at"]} for r in rows]
            return {"patterns": patterns, "recent_events": events}
        except Exception as e:
            log.error(f"Error fetching memory for {person_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/api/memory/{person_id}/event/{event_id}", dependencies=[Depends(_ac.verify_token)])
    async def delete_memory_event(person_id: str, event_id: int):
        db_person_id = person_registry.resolve(person_id) or person_id
        try:
            await db_writes.delete_memory_event(db_person_id, event_id)
            return {"ok": True}
        except Exception as e:
            log.error(f"Error deleting memory event {event_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/api/memory/{person_id}", dependencies=[Depends(_ac.verify_token)])
    async def delete_memory_all(person_id: str):
        db_person_id = person_registry.resolve(person_id) or person_id
        try:
            await db_writes.delete_memory_events_for_person(db_person_id)
            return {"ok": True}
        except Exception as e:
            log.error(f"Error clearing memory for {person_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # --- Ask Bernie ---
    @router.post("/api/ask", dependencies=[Depends(_ac.verify_token)])
    async def ask_bernie(data: Dict[str, str], auth_user: _ac.Person = Depends(_ac.verify_token)):
        question = data.get("question")
        if not question:
            raise HTTPException(status_code=400, detail="Missing question")
        start_time = datetime.now()
        
        real_name = person_registry.display_name(auth_user.id)
                
        model_name = _ac.config.get("webui_model")
        try:
            answer = await chat_general(
                question, [], _ac.config,
                person_name=real_name, triggered_by="web", model=model_name, 
                group=auth_user.role,
                actor_id=auth_user.id
            )
            latency = int((datetime.now() - start_time).total_seconds() * 1000)
            return {"answer": answer, "latency_ms": latency, "model": model_name}
        except Exception as e:
            log.error(f"Error in Ask Bernie: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # --- Network device update ---
    @router.put("/api/network/devices/{mac}", dependencies=[Depends(_ac.verify_token)])
    async def update_network_device(mac: str, data: Dict[str, Any]):
        try:
            from network_service import network_service
            await network_service.update_device(mac, data)
            return {"ok": True, "mac": mac}
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:
            log.error(f"Error updating device {mac}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/api/ha/entities", dependencies=[Depends(_ac.verify_token)])
    async def get_ha_entities(domain: str | None = None):
        try:
            states = await ha_service.get_live_states(domain=domain)
            return [
                {
                    "entity_id": s.get("entity_id"),
                    "state": s.get("state"),
                    "name": s.get("attributes", {}).get("friendly_name", s.get("entity_id")),
                    "domain": s.get("entity_id", ".").split(".")[0],
                    "attributes": {k: v for k, v in s.get("attributes", {}).items()
                                   if k in ("unit_of_measurement", "device_class", "friendly_name")}
                }
                for s in states
            ]
        except Exception as e:
            log.error(f"Error fetching HA entities: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/api/temperatures", dependencies=[Depends(_ac.verify_token)])
    async def get_temperatures(hours: int = 24):
        try:
            return await _get_temps_data(hours)
        except Exception as e:
            log.error(f"Error fetching temperatures: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    _TTL_SWITCHES   = 30
    _TTL_MEDIA      = 15
    _TTL_CLIMATE    = 60
    _TTL_AUTOS      = 60
    _TTL_SYSTEM     = 60
    _TTL_DASHBOARD  = 15

    async def _fetch_rooms_data():
        from collections import defaultdict
        live_lights = await ha_service.get_live_states(domain="light")
        config_map = {e["entity_id"]: e for e in _ac.config.get("home_assistant", {}).get("entities", [])}
        
        floor_mapping = {
            "child2": "UPSTAIRS",
            "dad": "UPSTAIRS",
            "mom": "UPSTAIRS",
            "master": "UPSTAIRS",
            "child1": "UPSTAIRS",
            "living_room": "MAIN",
            "kitchen": "MAIN",
            "dining": "MAIN",
            "basement": "BASEMENT",
            "exterior": "OUTSIDE"
        }
        
        floors = defaultdict(list)
        for s in live_lights:
            eid = s.get("entity_id", "")
            cfg_e = config_map.get(eid, {})
            room_key = cfg_e.get("room", "other")
            floor = floor_mapping.get(room_key.lower(), "OTHER")
            
            room_label = cfg_e.get("room_label", room_key.replace("_", " ").title()) if cfg_e else "Other"
            name = cfg_e.get("name") or s.get("attributes", {}).get("friendly_name") or eid.split(".")[-1].replace("_", " ").title()
            attrs = s.get("attributes", {})
            features = attrs.get("supported_features", 0)
            color_modes = attrs.get("supported_color_modes", [])
            floors[floor].append({
                "id": eid.replace("light.", "").replace("_", "-"),
                "name": name, "on": s.get("state") == "on", "room": room_label,
                "last_changed": s.get("last_changed", ""), "brightness": attrs.get("brightness"),
                "color_temp": attrs.get("color_temp_kelvin"), "rgb_color": attrs.get("rgb_color"),
                "supports_brightness": bool(features & HA_SUPPORT_BRIGHTNESS) or bool(set(color_modes) & HA_DIM_MODES),
                "supports_color_temp": bool(features & HA_SUPPORT_COLOR_TEMP) or "color_temp" in color_modes,
                "supports_rgb": bool(features & HA_SUPPORT_RGB_COLOR) or bool(set(color_modes) & HA_RGB_MODES),
            })
            
        order = {"UPSTAIRS": 0, "MAIN": 1, "BASEMENT": 2, "OUTSIDE": 3, "OTHER": 4}
        sorted_floors = sorted(floors.items(), key=lambda x: order.get(x[0], 99))
        return [{"name": f_name, "lights": sorted(lights, key=lambda l: l["name"])} for f_name, lights in sorted_floors]

    async def _get_temps_data(hours: int = 24):
        sensor_cfg = _ac.config.get("temperature_sensors", [])
        exclusions = [x.lower() for x in _ac.config.get("sensor_exclusions", ["octoprint"])]
        label_map = {}
        if sensor_cfg:
            all_live = await ha_service.get_live_states(domain="sensor")
            live_map = {s.get("entity_id"): s for s in all_live}
            sensors = []
            for entry in sensor_cfg:
                eid = entry.get("entity_id", "")
                if eid in live_map:
                    sensors.append(live_map[eid])
                    if entry.get("label"): label_map[eid] = entry["label"]
            if not sensors:
                sensors = [s for s in await ha_service.get_temperature_sensors()
                           if not any(x in s.get("entity_id", "").lower() for x in exclusions)]
        else:
            sensors = [s for s in await ha_service.get_temperature_sensors()
                       if not any(x in s.get("entity_id", "").lower() for x in exclusions)]
        histories = await asyncio.gather(*[ha_service.get_temperature_history(s.get("entity_id", ""), hours=hours) for s in sensors])
        result = []
        for s, history in zip(sensors, histories):
            eid = s.get("entity_id", "")
            name = label_map.get(eid) or s.get("attributes", {}).get("friendly_name", eid)
            temps = [float(h.get("state", "")) for h in history if h.get("state") not in (None, "unavailable", "unknown") and str(h.get("state", "")).replace(".", "").replace("-", "").isdigit()]
            result.append({
                "entity_id": eid, "name": name, "current": s.get("state"),
                "unit": s.get("attributes", {}).get("unit_of_measurement", "°C"),
                "min": round(min(temps), 1) if temps else None,
                "max": round(max(temps), 1) if temps else None,
                "history": [{"t": h.get("last_changed"), "v": h.get("state")} for h in history],
            })
        return result

    async def _fetch_switches():
        cfg = _ac.config.get("ha_switches", [])
        live = await ha_service.get_live_states(domain="switch")
        live_map = {s.get("entity_id"): s for s in live}
        result = []
        for entry in cfg:
            eid = entry["entity_id"]
            s = live_map.get(eid, {})
            result.append({
                "id": eid.replace("switch.", "").replace("_", "-"),
                "entity_id": eid,
                "name": entry.get("name") or s.get("attributes", {}).get("friendly_name", eid),
                "on": s.get("state") == "on",
                "room": entry.get("room", "other"),
                "room_label": entry.get("room_label", "Other"),
                "available": bool(s),
            })
        return result

    async def _fetch_media():
        cfg = _ac.config.get("ha_media_players", [])
        live = await ha_service.get_live_states(domain="media_player")
        live_map = {s.get("entity_id"): s for s in live}
        result = []
        for entry in cfg:
            eid = entry["entity_id"]
            s = live_map.get(eid, {})
            attrs = s.get("attributes", {})
            state = s.get("state", "unavailable")
            result.append({
                "id": eid.replace("media_player.", "").replace("_", "-"),
                "entity_id": eid,
                "name": entry.get("name") or attrs.get("friendly_name", eid),
                "state": state,
                "is_playing": state == "playing",
                "room": entry.get("room", "other"),
                "room_label": entry.get("room_label", "Other"),
                "media_title": attrs.get("media_title"),
                "media_artist": attrs.get("media_artist"),
                "volume": attrs.get("volume_level"),
                "muted": attrs.get("is_volume_muted", False),
                "available": state not in ("unavailable", "unknown"),
            })
        return result

    async def _fetch_climate():
        cfg = _ac.config.get("ha_climate_sensors", [])
        live = await ha_service.get_live_states(domain="sensor")
        live_map = {s.get("entity_id"): s for s in live}
        result = []
        for entry in cfg:
            eid = entry["entity_id"]
            s = live_map.get(eid, {})
            attrs = s.get("attributes", {})
            result.append({
                "entity_id": eid,
                "name": entry.get("name") or attrs.get("friendly_name", eid),
                "value": s.get("state"),
                "unit": entry.get("unit") or attrs.get("unit_of_measurement", ""),
                "icon": entry.get("icon", "sensor"),
                "room": entry.get("room", "other"),
                "room_label": entry.get("room_label", "Other"),
                "available": bool(s) and s.get("state") not in ("unavailable", "unknown"),
            })
        return result

    async def _fetch_automations():
        cfg = _ac.config.get("ha_automations", [])
        live = await ha_service.get_live_states(domain="automation")
        live_map = {s.get("entity_id"): s for s in live}
        result = []
        for entry in cfg:
            eid = entry["entity_id"]
            s = live_map.get(eid, {})
            result.append({
                "id": eid.replace("automation.", "").replace("_", "-"),
                "entity_id": eid,
                "name": entry.get("name") or s.get("attributes", {}).get("friendly_name", eid),
                "enabled": s.get("state") == "on",
                "available": bool(s),
            })
        return result

    async def _fetch_system():
        cfg = _ac.config.get("ha_system_entities", [])
        live_sensor = await ha_service.get_live_states(domain="sensor")
        live_binary = await ha_service.get_live_states(domain="binary_sensor")
        live_update = await ha_service.get_live_states(domain="update")
        live_map = {s.get("entity_id"): s for s in live_sensor + live_binary + live_update}
        result = []
        for entry in cfg:
            eid = entry["entity_id"]
            s = live_map.get(eid, {})
            attrs = s.get("attributes", {})
            state = s.get("state", "unavailable")
            result.append({
                "entity_id": eid,
                "name": entry.get("name") or attrs.get("friendly_name", eid),
                "state": state,
                "group": entry.get("group", "other"),
                "available": state not in ("unavailable", "unknown"),
                "unit": attrs.get("unit_of_measurement", ""),
                "installed_version": attrs.get("installed_version"),
                "latest_version": attrs.get("latest_version"),
            })
        return result

    # --- Switches ---
    @router.get("/api/switches", dependencies=[Depends(_ac.verify_token)])
    async def get_switches():
        try:
            cached = _ac._cache_get("home_switches", _TTL_SWITCHES)
            if cached is not None: return cached
            result = await _fetch_switches()
            _ac._cache_set("home_switches", result)
            return result
        except Exception as e:
            log.error(f"Switches error: {e}"); raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/switches/{id}", dependencies=[Depends(_ac.verify_token)])
    async def toggle_switch(id: str, req: _ac.SwitchControl):
        entity_id = f"switch.{id.replace('-', '_')}"
        if req.on is True:
            success = await ha_service.turn_on(entity_id)
        elif req.on is False:
            success = await ha_service.turn_off(entity_id)
        else:
            success = await ha_service.toggle(entity_id)
        _invalidate_home_caches()
        if success: return {"ok": True}
        raise HTTPException(status_code=500, detail="Failed to control switch")

    # --- Media players ---
    @router.get("/api/media", dependencies=[Depends(_ac.verify_token)])
    async def get_media():
        try:
            cached = _ac._cache_get("home_media", _TTL_MEDIA)
            if cached is not None: return cached
            result = await _fetch_media()
            _ac._cache_set("home_media", result)
            return result
        except Exception as e:
            log.error(f"Media error: {e}"); raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/media/{id}", dependencies=[Depends(_ac.verify_token)])
    async def control_media(id: str, req: _ac.MediaCommand):
        entity_id = f"media_player.{id.replace('-', '_')}"
        success = await ha_service.media_control(entity_id, req.command, volume=req.volume)
        _invalidate_home_caches()
        if success: return {"ok": True}
        raise HTTPException(status_code=500, detail="Failed to control media player")

    # --- Climate sensors (humidity, CO2, PM2.5, VOC) ---
    @router.get("/api/climate", dependencies=[Depends(_ac.verify_token)])
    async def get_climate():
        try:
            cached = _ac._cache_get("home_climate", _TTL_CLIMATE)
            if cached is not None: return cached
            result = await _fetch_climate()
            _ac._cache_set("home_climate", result)
            return result
        except Exception as e:
            log.error(f"Climate error: {e}"); raise HTTPException(status_code=500, detail=str(e))

    # --- HA Automations (family-bot-mu2.1: distinct path from Bernie /api/automations CRUD) ---
    @router.get("/api/ha/automations", dependencies=[Depends(_ac.verify_token)])
    async def get_ha_automations():
        try:
            cached = _ac._cache_get("home_automations", _TTL_AUTOS)
            if cached is not None: return cached
            result = await _fetch_automations()
            _ac._cache_set("home_automations", result)
            return result
        except Exception as e:
            log.error(f"HA Automations error: {e}"); raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/ha/automations/{id}/toggle", dependencies=[Depends(_ac.verify_token)])
    async def toggle_ha_automation(id: str):
        entity_id = f"automation.{id.replace('-', '_')}"
        success = await ha_service.toggle(entity_id)
        _invalidate_home_caches()
        if success: return {"ok": True}
        raise HTTPException(status_code=500, detail="Failed to toggle automation")

    # --- System entities ---
    @router.get("/api/system", dependencies=[Depends(_ac.verify_token)])
    async def get_system():
        try:
            cached = _ac._cache_get("home_system", _TTL_SYSTEM)
            if cached is not None: return cached
            result = await _fetch_system()
            _ac._cache_set("home_system", result)
            return result
        except Exception as e:
            log.error(f"System error: {e}"); raise HTTPException(status_code=500, detail=str(e))

    @router.post("/api/system/check-updates", dependencies=[Depends(_ac.verify_token)])
    async def check_updates():
        try:
            cfg = _ac.config.get("ha_system_entities", [])
            update_eids = [e["entity_id"] for e in cfg if e["entity_id"].startswith("update.")]
            results = await asyncio.gather(
                *[ha_service._call_service("homeassistant", "update_entity", eid) for eid in update_eids],
                return_exceptions=True
            )
            _invalidate_home_caches()
            refreshed = sum(1 for r in results if r is True)
            return {"ok": True, "refreshed": refreshed}
        except Exception as e:
            log.error(f"Check updates error: {e}"); raise HTTPException(status_code=500, detail=str(e))

    # --- Combined home dashboard (single-request load) ---
    @router.get("/api/home/dashboard", dependencies=[Depends(_ac.verify_token)])
    async def get_home_dashboard(hours: int = 24):
        try:
            cached = _ac._cache_get("home_dashboard", _TTL_DASHBOARD)
            if cached is not None: return cached

            safe = lambda coro: asyncio.ensure_future(coro)
            rooms_f    = safe(_fetch_rooms_data())
            switches_f = safe(_fetch_switches())
            media_f    = safe(_fetch_media())
            climate_f  = safe(_fetch_climate())
            autos_f    = safe(_fetch_automations())
            system_f   = safe(_fetch_system())
            temps_f    = safe(_get_temps_data(hours))

            results = await asyncio.gather(
                rooms_f, switches_f, media_f, climate_f, autos_f, system_f, temps_f,
                return_exceptions=True
            )

            def safe_result(r, default):
                return r if not isinstance(r, Exception) else default

            payload = {
                "rooms":       safe_result(results[0], []),
                "switches":    safe_result(results[1], []),
                "media":       safe_result(results[2], []),
                "climate":     safe_result(results[3], []),
                "automations": safe_result(results[4], []),
                "system":      safe_result(results[5], []),
                "temps":       safe_result(results[6], []),
            }
            _ac._cache_set("home_dashboard", payload)
            return payload
        except Exception as e:
            log.error(f"Dashboard error: {e}"); raise HTTPException(status_code=500, detail=str(e))

    @router.get("/api/activity/notifications", dependencies=[Depends(_ac.verify_token)])
    async def get_notifications(limit: int = 20):
        try:
            entries = await db.get_notification_log(limit)
            for e in entries:
                discord_id = str(e.get("who", ""))
                person_id = person_registry.resolve(discord_id)
                e["who"] = person_registry.display_name(person_id) if person_id else "—"
            return entries
        except Exception as e:
            log.error(f"Error fetching notification log: {e}")
            raise HTTPException(status_code=500, detail=str(e))


    return router
