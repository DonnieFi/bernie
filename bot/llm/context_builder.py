"""Context building for LLM turns (perf + 4.4).

Moved build_context here with per-leg timing instrumentation.
Uses LiveSnapshot BTS when fresh; calendar lazy by default (tools only).
"""

from __future__ import annotations

import time as _time
import logging

log = logging.getLogger(__name__)


from dataclasses import dataclass
from typing import Any
import db_writes

@dataclass
class LegResult:
    data: Any
    ms: int


async def build_context(
    config: dict,
    cal_service=None,
    session=None,
    user_message: str = "",
    channel_id: str = "",
    is_dm: bool = False,
    mode: str = "",
) -> dict:
    """Assemble live context using leg planner + snapshot + gather."""
    import asyncio as _aio
    from .context_legs import should_prefetch_calendar, should_prefetch_weather
    from services.live_snapshot import ensure_fresh_snapshot, get_live_snapshot

    ctx: dict = {}
    _t0 = _time.monotonic()

    from llm.context_legs import looks_home_intent

    do_cal = should_prefetch_calendar(channel_id, is_dm, mode, user_message, config)
    do_weather = should_prefetch_weather(channel_id, is_dm, mode, user_message, config)
    ctx_cfg = config.get("context", {}) or {}
    # family-bot-2wh.10: honor context.prefetch.ha (always|intent|never)
    ha_mode = str((ctx_cfg.get("prefetch") or {}).get("ha", "always")).lower()
    if ha_mode == "never":
        do_ha = False
    elif ha_mode == "intent":
        do_ha = looks_home_intent(user_message or "")
    else:
        do_ha = True  # always (default brownfield)

    snap = await ensure_fresh_snapshot(
        config=config, cal_service=cal_service, session=session,
    )
    max_age = float(ctx_cfg.get("snapshot_refresh_min", 5)) * 60.0
    snap_fresh = snap is not None and snap.is_fresh(max_age)

    async def _get_presence():
        _p0 = _time.monotonic()
        if snap_fresh and snap is not None:
            return LegResult(snap.presence, 0)
        try:
            from presence_service import presence_service
            res = await presence_service.get_presence()
            return LegResult(res, int((_time.monotonic() - _p0) * 1000))
        except Exception as e:
            log.warning("build_context: presence error: %s", e)
            return LegResult({}, int((_time.monotonic() - _p0) * 1000))

    async def _get_ha():
        _h0 = _time.monotonic()
        if snap_fresh and snap is not None:
            return LegResult(snap.ha_states, 0)
        try:
            from ha_service import ha_service
            ha_domains = ctx_cfg.get("ha_domains") or ["light", "switch", "media_player"]
            live = await ha_service.get_live_states()
            res = [
                {"entity_id": s.get("entity_id"), "state": s.get("state"),
                 "name": (s.get("attributes") or {}).get(
                     "friendly_name", s.get("entity_id"),
                 )}
                for s in live if s.get("entity_id", "").split(".")[0] in ha_domains
            ]
            return LegResult(res, int((_time.monotonic() - _h0) * 1000))
        except Exception as e:
            log.warning("build_context: HA states error: %s", e)
            return LegResult([], int((_time.monotonic() - _h0) * 1000))

    async def _get_calendar():
        _c0 = _time.monotonic()
        try:
            if cal_service and do_cal:
                from school_calendar import exclude_school_from_schedule

                raw_events = await cal_service.get_todays_events()
                events = exclude_school_from_schedule(raw_events, config)
                res = cal_service.events_to_text(events)
            else:
                res = ""
            return LegResult(res, int((_time.monotonic() - _c0) * 1000))
        except Exception as e:
            log.warning("build_context: calendar error: %s", e)
            return LegResult("", int((_time.monotonic() - _c0) * 1000))

    async def _get_weather():
        _w0 = _time.monotonic()
        if snap_fresh and snap and snap.weather and do_weather:
            return LegResult(snap.weather, 0)
        try:
            if session and do_weather:
                from weather_service import get_weather, weather_line
                lat = config.get("location", {}).get("lat", 44.6476)
                lon = config.get("location", {}).get("lon", -63.5728)
                w = await get_weather(lat, lon, session)
                res = weather_line(w) if w else ""
            else:
                res = ""
            return LegResult(res, int((_time.monotonic() - _w0) * 1000))
        except Exception as e:
            log.warning("build_context: weather error: %s", e)
            return LegResult("", int((_time.monotonic() - _w0) * 1000))

    tasks = [_get_presence()]
    ha_task_idx = None
    if do_ha:
        ha_task_idx = len(tasks)
        tasks.append(_get_ha())
    cal_task_idx = None
    if do_cal:
        cal_task_idx = len(tasks)
        tasks.append(_get_calendar())
    weather_task_idx = None
    if do_weather:
        weather_task_idx = len(tasks)
        tasks.append(_get_weather())

    results = await _aio.gather(*tasks, return_exceptions=True)

    def _get_leg(idx, default):
        if idx is None or idx < 0 or idx >= len(results):
            return default
        r = results[idx]
        if isinstance(r, BaseException):
            log.warning("build_context leg failed: %s", r)
            return default
        return r if isinstance(r, LegResult) else default

    pres_res = _get_leg(0, LegResult({}, 0))
    ha_res = _get_leg(ha_task_idx, LegResult([], 0)) if do_ha else LegResult([], 0)
    cal_res = _get_leg(cal_task_idx, LegResult("", 0)) if do_cal else LegResult("", 0)
    w_res = _get_leg(weather_task_idx, LegResult("", 0)) if do_weather else LegResult("", 0)

    ctx["presence"] = pres_res.data if isinstance(pres_res.data, dict) else {}
    ctx["ha_states"] = ha_res.data if isinstance(ha_res.data, list) else []
    ctx["today_events"] = cal_res.data if isinstance(cal_res.data, str) else ""
    ctx["weather"] = w_res.data if isinstance(w_res.data, str) else ""
    ctx["calendar_lazy"] = not do_cal
    # family-bot-2wh.2: surface user message for BernieContext mode resolution
    if user_message:
        ctx["last_user_message"] = user_message

    _total = int((_time.monotonic() - _t0) * 1000)

    try:
        from telemetry import fire_and_forget
        from db_binding import get_database
        from llm.turn_timer import TurnTimer
        _timer = TurnTimer.current()
        _turn = getattr(_timer, "turn_id", None) if _timer else None
        _chan = getattr(_timer, "channel_id", None) if _timer else None
        _pers = getattr(_timer, "person_id", None) if _timer else None
        if _timer:
            _timer.record("context", _total)
            _timer.advance()
        fire_and_forget(db_writes.routed("log_context_build", 
            turn_id=_turn,
            presence_ms=pres_res.ms,
            ha_ms=ha_res.ms,
            calendar_ms=cal_res.ms if do_cal else 0,
            weather_ms=w_res.ms if do_weather else 0,
            total_ms=_total,
            channel_id=_chan,
            person_id=_pers,
            calendar_cache_hit=(
                getattr(cal_service, "_last_calendar_cache_hit", None)
                if do_cal and cal_service is not None else None
            ),
        ))
    except Exception:
        pass

    return ctx
