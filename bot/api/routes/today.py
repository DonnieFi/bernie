"""API routes: today (family-bot-8lx.2 hard-cut)."""
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


def build_today_router(ctx: Any) -> APIRouter:
    """Register today routes; closes over container services via ctx."""
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

    @router.get("/api/today", dependencies=[Depends(_ac.verify_token)])
    async def get_today():
        _t0 = time.monotonic()
        _TZ = ZoneInfo(_ac.config.get("timezone", "America/Halifax"))
        now = datetime.now(_TZ)
        lat, lon = _ac.config.get("location", {}).get("lat", 44.6476), _ac.config.get("location", {}).get("lon", -63.5728)

        session = http_session
        # Weather & Events
        async def _get_w():
            return await get_weather(lat, lon, session)

        async def _get_events():
            if now.hour >= 20:
                try:
                    evs = await calendar_service.get_tomorrows_events()
                except Exception as e:
                    log.warning(f"Calendar fetch error (tomorrow): {e}")
                    evs = []
                return evs, "Tomorrow"
            else:
                try:
                    evs = await calendar_service.get_todays_events()
                except Exception as e:
                    log.warning(f"Calendar fetch error (today): {e}")
                    evs = []
                return evs, "Evening" if now.hour >= 17 else "Today"

        async def _get_sun():
            try:
                return await ha_service.get_state("sun.sun")
            except Exception:
                return {}

        w, (events, schedule_label), sun_state = await asyncio.gather(
            _get_w(), _get_events(), _get_sun()
        )

        from school_calendar import exclude_school_from_schedule

        events = exclude_school_from_schedule(events, _ac.config)

        # Highlights
        _th = time.monotonic()
        h_key = f"highlights:{now.strftime('%Y-%m-%d')}"
        f_h = _ac._cache_get(h_key, _ac._TTL_HIGHLIGHTS)
        if not f_h:
            try:
                f_h = await _ac._build_formatted_highlights(session, _ac.config, calendar_service, summary_builder, _TZ, w, events)
                _ac._cache_set(h_key, f_h)
            except Exception as e:
                log.warning(f"Highlights build failed: {e}")
                f_h = []
        log.debug(f"/api/today highlights: {time.monotonic()-_th:.3f}s")

        # Presence
        _tp = time.monotonic()
        try:
            pres_full = await asyncio.wait_for(presence_service.get_full_presence(), timeout=8.0)
        except asyncio.TimeoutError:
            log.warning("/api/today: presence timed out after 8s")
            pres_full = {}
        except Exception as e:
            log.warning(f"Presence fetch failed: {e}")
            pres_full = {}
        log.debug(f"/api/today presence: {time.monotonic()-_tp:.3f}s")
        tracker_cfg = _ac.config.get("presence", {}).get("device_trackers", {})
        presence_list = []
        for p_id, info in pres_full.items():
            person = person_registry.get(p_id)
            if not person or person.get("role") == "friend":
                continue
            display_name = person.get("display", p_id.capitalize())
            is_home = info.get("home", False)
            ls = info.get("last_seen")
            ls_str = ""
            if ls:
                try: ls_str = datetime.fromisoformat(str(ls)).astimezone(_TZ).strftime("%-I:%M %p")
                except Exception: pass
            
            # Check for conflict label first, otherwise use status_label (Home/Away/Zone)
            conflict = info.get("conflict_label")
            status_label = info.get("status_label", "Home" if is_home else "Away")
            
            if conflict:
                sub = conflict
            else:
                sub = f"{status_label} since {ls_str}" if ls_str else status_label
            
            # Tracked = has a real device tracker (battery) OR WiFi MACs
            tracker = tracker_cfg.get(p_id, {})
            tracked = bool(tracker.get("battery_sensor")) or (person and bool(person.get("device_macs")))

            presence_list.append({
                "id": p_id, "name": display_name, "initial": display_name[0].upper(),
                "home": is_home, "departing": info.get("departing", False), "wifi": info.get("wifi", False), "essid": info.get("essid"),
                "status_label": status_label,
                "sub": sub, "tracked": tracked, "last_seen_ts": str(ls) if ls else None
            })

        # Schedule
        def _h_str(h): return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"
        sched = {_h_str(h): [] for h in range(8, 23)}
        for ev in events:
            if ev.get("all_day"): continue
            start = ev["start"]
            if hasattr(start, "tzinfo") and start.tzinfo: start = start.astimezone(_TZ)
            if 8 <= start.hour <= 22:
                sched[_h_str(start.hour)].append({"title": ev["summary"], "sub": ev.get("location", ""), "time": start.strftime("%-I:%M %p").lower()})

        # Weather Detail
        temp = round(w.get("temp_c", 0)) if w else 0
        cond = w.get("condition", "Unknown") if w else "Unknown"

        # Hourly
        hourly_list = []
        if w:
            for h_entry in w.get("hourly", []):
                hr_num = h_entry.get("hour", 12)
                p_prob = h_entry.get("precip_prob_pct", 0)
                raw_cond = h_entry.get("condition", cond)
                if raw_cond == "—": raw_cond = cond
                h_cond = raw_cond.lower()
                icon = "cloud"
                if "fog" in h_cond or "mist" in h_cond: icon = "fog"
                elif any(x in h_cond for x in ["thunder", "storm"]): icon = "storm"
                elif p_prob > 40 or "rain" in h_cond or "shower" in h_cond or "drizzle" in h_cond: icon = "drizzle"
                elif "snow" in h_cond or "flurries" in h_cond or "blizzard" in h_cond: icon = "snow"
                elif any(x in h_cond for x in ["clear", "sunny"]) and not any(x in h_cond for x in ["cloud", "overcast", "partly"]): icon = "sun"
                hourly_list.append({"hr": _h_str(hr_num), "temp": round(h_entry.get("temp_c") or temp), "icon": icon, "now": hr_num == now.hour})

        # Sunset — label as tomorrow when sun is already below horizon
        sunset_str = ""
        sunset_tomorrow = False
        if sun_state and "next_setting" in sun_state.get("attributes", {}):
            try:
                sunset_str = datetime.fromisoformat(sun_state["attributes"]["next_setting"].replace("Z", "+00:00")).astimezone(_TZ).strftime("%H:%M")
                sunset_tomorrow = sun_state.get("state") == "below_horizon"
            except Exception: pass

        # Derive weather mood from conditions
        cond_lower = cond.lower()
        now_hr_entry = next((h for h in (w.get("hourly", []) if w else []) if h.get("hour") == now.hour), None)
        cur_precip = now_hr_entry.get("precip_prob_pct", 0) if now_hr_entry else 0
        if any(x in cond_lower for x in ["thunder", "storm"]): mood = "stormy"
        elif cur_precip > 40 or any(x in cond_lower for x in ["rain", "shower", "drizzle"]): mood = "rainy"
        elif any(x in cond_lower for x in ["fog", "mist"]): mood = "foggy"
        elif any(x in cond_lower for x in ["snow", "flurries", "blizzard"]): mood = "snowy"
        elif any(x in cond_lower for x in ["clear", "sunny"]) and not any(x in cond_lower for x in ["cloud", "partly"]): mood = "sunny"
        elif any(x in cond_lower for x in ["cloud", "overcast"]): mood = "cloudy"
        else: mood = "default"

        # bernieNote — return stale immediately, refresh in background
        note_key = f"{now.strftime('%Y-%m-%d')}:{schedule_label.lower()}"
        b_note = _ac._note_cache.get(note_key, "Bernie is ready.")
        _last_refresh = _ac._note_refresh_ts.get(note_key, 0.0)
        if note_key not in _ac._note_inflight and time.monotonic() - _last_refresh > _ac._NOTE_REFRESH_INTERVAL:
            async def _refresh_note(
                key=note_key,
                sched_label=schedule_label,
                events=events,
                cond=cond,
                temp=temp,
            ):
                try:
                    from llm.chat import chat_general

                    events_text = calendar_service.events_to_text(events)
                    period = sched_label.lower()
                    note_live_context = {
                        "schedule_label": sched_label,
                        "today_events": events_text,
                        "weather": f"{cond}, {temp}°C",
                        "presence": {},
                        "ha_states": [],
                    }
                    prompt = (
                        f"Write ONE concise, friendly sentence for the family dashboard "
                        f"about {period}. Use ONLY the {sched_label} schedule in context — "
                        "do not mention events from other days. "
                        "If there are no events, say it's a quiet day."
                    )
                    note = await chat_general(
                        prompt,
                        [],
                        _ac.config,
                        triggered_by="web",
                        group="system",
                        actor_id="bernie",
                        suppress_shadow=True,
                        live_context_override=note_live_context,
                    )
                    if note:
                        _ac._note_cache[key] = note
                        _ac._note_refresh_ts[key] = time.monotonic()  # throttle only advances on success
                except Exception as e:
                    log.warning(f"bernieNote background refresh failed: {e}")
                finally:
                    _ac._note_inflight.discard(key)  # always unblock, retry on next request if failed
            _ac._note_inflight.add(note_key)
            asyncio.create_task(_refresh_note())

        _tg = time.monotonic()
        garbage = None
        ics = _ac.config.get("recollect_ics_url")
        if ics:
            try:
                colls = await get_next_collections(ics, _TZ, session, days=7)
                if colls: garbage = {"date_label": "Soon", "summary": colls[0]["summary"], "icon": colls[0]["icon"]}
            except Exception: pass
        log.debug(f"/api/today garbage: {time.monotonic()-_tg:.3f}s")
        log.info(f"/api/today total: {time.monotonic()-_t0:.3f}s")

        webui_user_name = person_registry.display_name(_ac.config.get("webui_user", "dad"))
        return {
            "now": {"h": now.hour, "m": now.minute}, "today": now.strftime("%A, %B %-d"),
            "user": {"name": webui_user_name}, "presence": presence_list,
            "highlights": f_h, "bernieNote": b_note, "garbage": garbage, "cameras": _frigate.cameras,
            "schedule": [{"hour": k, "events": v} for k, v in sched.items()],
            "schedule_label": schedule_label,
            "weather": {
                "temp": temp, "feelsLike": round(w.get("feels_like_c") or temp) if w else temp,
                "dewpoint_c": w.get("dewpoint_c") if w else None,
                "condition": cond, "wind_dir": w.get("wind_dir", "") if w else "",
                "wind_kmh": round(w.get("wind_kmh") or 0) if w else 0, "hourly": hourly_list,
                "mood": mood, "sunset": sunset_str, "sunset_tomorrow": sunset_tomorrow,
                "location_label": "Halifax, NS"
            }
        }

    @router.get("/api/family", dependencies=[Depends(_ac.verify_token)])
    async def get_family():
        memory_counts = {}
        try:
            memory_counts = await db.count_memory_events_by_person()
        except Exception as e:
            log.error(f"Failed to fetch memory counts: {e}")

        try:
            presence = await asyncio.wait_for(presence_service.get_full_presence(), timeout=10.0)
        except asyncio.TimeoutError:
            log.warning("/api/family: presence timed out after 10s")
            presence = {}
        except Exception as e:
            log.warning(f"get_family: presence fetch failed: {e}")
            presence = {}
            
        result = []
        for person in person_registry.family():
            p_id = person["id"]
            display_name = person["display"]
            pres = presence.get(p_id, {})
            result.append({
                "who": p_id,
                "name": display_name,
                "initial": display_name[0].upper(),
                "role": person.get("role", ""),
                "email": person.get("email", ""),
                "home": pres.get("home", False),
                "departing": pres.get("departing", False),
                "wifi": pres.get("wifi", False),
                "essid": pres.get("essid"),
                "status": pres.get("status_label", "Away"),
                "status_label": pres.get("status_label", "Away"),
                "last_seen": pres.get("last_seen"),
                "battery": pres.get("battery", 0),
                "gps": pres.get("gps"),
                "address": pres.get("address"),
                "memory_count": memory_counts.get(p_id, 0)
            })
        return result

    @router.post("/api/presence/refresh", dependencies=[Depends(_ac.verify_token)])
    async def refresh_presence_api():
        await presence_service.refresh_presence()
        return {"ok": True}


    return router
