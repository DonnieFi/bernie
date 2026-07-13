"""FlightAware AeroAPI flight status service for Bernie.

Endpoints (https://aeroapi.flightaware.com/aeroapi):
- GET /flights/{ident} — one credit-efficient lookup for schedule, delays, route,
  gates, and OOOI times. Always called first; max_pages=1 keeps us to one result set.
- GET /flights/{fa_flight_id}/position — live lat/lon/alt/speed/heading. Only called
  when the selected leg looks en route (departed, not yet arrived) to save credits.

Auth: x-apikey header from env FLIGHT_AERO_KEY.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field

from transit_service import maps_link, maps_link_block, static_map_url

log = logging.getLogger(__name__)

AEROAPI_BASE = "https://aeroapi.flightaware.com/aeroapi"
_CACHE_TTL_S = 180
_CACHE_MAX = 64
_cache: dict[str, tuple[float, "FlightStatusResult"]] = {}
_inflight: dict[str, asyncio.Future] = {}

# ponytail: small coord table for relative position; expand or wire geocoder later
_AIRPORT_COORDS: dict[str, tuple[float, float]] = {
    "YHZ": (44.8808, -63.5086),
    "FRA": (50.0379, 8.5622),
    "EDDF": (50.0379, 8.5622),
    "YYZ": (43.6777, -79.6248),
    "CYYZ": (43.6777, -79.6248),
    "JFK": (40.6413, -73.7781),
    "KJFK": (40.6413, -73.7781),
    "LHR": (51.4700, -0.4543),
    "EGLL": (51.4700, -0.4543),
}


class FlightPhase(str, Enum):
    scheduled = "scheduled"
    en_route = "en_route"
    landed = "landed"
    cancelled = "cancelled"
    diverted = "diverted"
    unknown = "unknown"


class FlightPosition(BaseModel):
    latitude: float
    longitude: float
    altitude_ft: int | None = None
    ground_speed_kts: int | None = None
    heading_deg: int | None = None
    position_time: str | None = None


class FlightTimes(BaseModel):
    scheduled_departure: str | None = None
    estimated_departure: str | None = None
    actual_departure: str | None = None
    scheduled_arrival: str | None = None
    estimated_arrival: str | None = None
    actual_arrival: str | None = None
    origin_timezone: str | None = None
    destination_timezone: str | None = None


class FlightStatusResult(BaseModel):
    flight_number: str
    ident: str
    fa_flight_id: str | None = None
    phase: FlightPhase
    status_detail: str | None = None
    route: str | None = None
    origin_code: str | None = None
    destination_code: str | None = None
    times: FlightTimes = Field(default_factory=FlightTimes)
    position: FlightPosition | None = None
    relative_position: str | None = None
    remaining_minutes: int | None = None
    remaining_display: str | None = None
    progress_percent: int | None = None
    map_url: str | None = None  # FlightAware live track
    map_latitude: float | None = None
    map_longitude: float | None = None
    google_maps_url: str | None = None
    static_map_image_url: str | None = None
    summary: str = ""
    raw_flight: dict[str, Any] | None = None
    raw_position: dict[str, Any] | None = None


def _api_key() -> str | None:
    return os.environ.get("FLIGHT_AERO_KEY", "").strip() or None


def _normalize_ident(flight_number: str) -> str:
    return re.sub(r"\s+", "", flight_number.strip().upper())


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # AeroAPI sometimes omits offset — treat naive as UTC so subtractions don't TypeError
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_local(iso: str | None, tz_name: str | None) -> str | None:
    dt = _parse_dt(iso)
    if dt is None:
        return None
    tz = ZoneInfo(tz_name or "UTC")
    local = dt.astimezone(tz)
    return local.strftime(f"%Y-%m-%d %H:%M %Z")


def _airport_code(ref: dict | None) -> str | None:
    if not ref:
        return None
    return ref.get("code_iata") or ref.get("code_icao") or ref.get("code")


def _airport_tz(ref: dict | None) -> str | None:
    if not ref:
        return None
    return ref.get("timezone")


def _coords(code: str | None) -> tuple[float, float] | None:
    if not code:
        return None
    return _AIRPORT_COORDS.get(code.upper())


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _bearing_cardinal(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    deg = (math.degrees(math.atan2(x, y)) + 360) % 360
    dirs = ["north", "NNE", "NE", "ENE", "east", "ESE", "SE", "SSE",
            "south", "SSW", "SW", "WSW", "west", "WNW", "NW", "NNW"]
    return dirs[int((deg + 11.25) / 22.5) % 16]


def _derive_phase(flight: dict) -> FlightPhase:
    if flight.get("cancelled"):
        return FlightPhase.cancelled
    if flight.get("diverted"):
        return FlightPhase.diverted
    if flight.get("actual_in") or flight.get("actual_on"):
        return FlightPhase.landed
    if flight.get("actual_off") or flight.get("actual_out"):
        return FlightPhase.en_route
    st = (flight.get("status") or "").lower()
    if "cancel" in st:
        return FlightPhase.cancelled
    if "divert" in st:
        return FlightPhase.diverted
    if any(x in st for x in ("en route", "enroute", "airborne", "active")):
        return FlightPhase.en_route
    if "arriv" in st or "landed" in st:
        return FlightPhase.landed
    if "sched" in st or "filed" in st or "gate" in st:
        return FlightPhase.scheduled
    return FlightPhase.unknown


def _sched_dt(flight: dict) -> datetime | None:
    for key in ("scheduled_out", "scheduled_off", "estimated_out", "estimated_off"):
        dt = _parse_dt(flight.get(key))
        if dt:
            return dt
    return None


def _pick_flight(flights: list[dict]) -> dict | None:
    if not flights:
        return None
    now = datetime.now(timezone.utc)
    window = timedelta(hours=72)
    candidates = [f for f in flights if _sched_dt(f) and abs(_sched_dt(f) - now) <= window]
    pool = candidates or flights

    def sort_key(f: dict) -> tuple[int, float]:
        phase = _derive_phase(f)
        sd = _sched_dt(f) or now
        if phase == FlightPhase.en_route:
            return (0, -sd.timestamp())
        if phase == FlightPhase.scheduled:
            return (1, abs((sd - now).total_seconds()))
        if phase == FlightPhase.landed:
            arr = _parse_dt(f.get("actual_on") or f.get("actual_in")) or sd
            return (2, -arr.timestamp())
        if phase == FlightPhase.cancelled:
            return (4, -sd.timestamp())
        return (3, -sd.timestamp())

    return min(pool, key=sort_key)


def _remaining_minutes(flight: dict) -> int | None:
    eta = _parse_dt(
        flight.get("estimated_in")
        or flight.get("estimated_on")
        or flight.get("scheduled_in")
        or flight.get("scheduled_on")
    )
    if not eta:
        return None
    delta = eta - datetime.now(timezone.utc)
    return max(0, int(delta.total_seconds() // 60))


def _format_remaining(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m"


def _describe_position(
    lat: float,
    lon: float,
    alt_ft: int | None,
    dest_code: str | None,
    dest_coords: tuple[float, float] | None,
) -> str:
    alt_part = f"FL{alt_ft // 100}" if alt_ft and alt_ft >= 1000 else (
        f"{alt_ft} ft" if alt_ft else "altitude unknown"
    )
    # North Atlantic heuristic (FRA→YHZ-style tracks)
    if 40 <= lat <= 60 and -60 <= lon <= -10 and dest_code == "YHZ":
        dist = _haversine_km(lat, lon, *_AIRPORT_COORDS["YHZ"])
        if dist > 100:
            return f"Over the North Atlantic, ~{dist:.0f} km east of Halifax, cruising at {alt_part}"
    if dest_coords:
        dist = _haversine_km(lat, lon, *dest_coords)
        card = _bearing_cardinal(lat, lon, *dest_coords)
        label = dest_code or "destination"
        return f"~{dist:.0f} km {card} of {label}, cruising at {alt_part}"
    return f"At {lat:.3f}°, {lon:.3f}°, {alt_part}"


def _map_url(flight: dict) -> str | None:
    ident = flight.get("ident_icao") or flight.get("ident") or flight.get("ident_iata")
    if not ident:
        return None
    return f"https://flightaware.com/live/flight/{ident}"


def _resolve_map_pin(
    phase: FlightPhase,
    position: FlightPosition | None,
    origin_code: str | None,
    dest_code: str | None,
) -> tuple[float, float, int] | None:
    """Lat/lon/zoom for OSM + Google Maps (same pattern as transit / presence)."""
    if position is not None:
        zoom = 6 if phase == FlightPhase.en_route else 10
        return position.latitude, position.longitude, zoom
    if phase == FlightPhase.landed:
        coords = _coords(dest_code)
        if coords:
            return coords[0], coords[1], 10
    if phase == FlightPhase.scheduled:
        coords = _coords(origin_code)
        if coords:
            return coords[0], coords[1], 10
    return None


def _build_times(flight: dict) -> FlightTimes:
    origin = flight.get("origin") or {}
    dest = flight.get("destination") or {}
    otz, dtz = _airport_tz(origin), _airport_tz(dest)
    dep_iso = flight.get("actual_out") or flight.get("actual_off")
    arr_iso = flight.get("actual_in") or flight.get("actual_on")
    return FlightTimes(
        scheduled_departure=_fmt_local(flight.get("scheduled_out") or flight.get("scheduled_off"), otz),
        estimated_departure=_fmt_local(flight.get("estimated_out") or flight.get("estimated_off"), otz),
        actual_departure=_fmt_local(dep_iso, otz),
        scheduled_arrival=_fmt_local(flight.get("scheduled_in") or flight.get("scheduled_on"), dtz),
        estimated_arrival=_fmt_local(flight.get("estimated_in") or flight.get("estimated_on"), dtz),
        actual_arrival=_fmt_local(arr_iso, dtz),
        origin_timezone=otz,
        destination_timezone=dtz,
    )


def _build_summary(result: FlightStatusResult) -> str:
    lines = [f"**{result.ident}** ({result.route or 'route unknown'}) — **{result.phase.value.replace('_', ' ')}**"]
    if result.status_detail:
        lines.append(result.status_detail)
    t = result.times
    if result.phase == FlightPhase.scheduled:
        if t.scheduled_departure:
            lines.append(f"Scheduled departure: {t.scheduled_departure}")
        if t.estimated_departure and t.estimated_departure != t.scheduled_departure:
            lines.append(f"Estimated departure: {t.estimated_departure}")
        if t.scheduled_arrival:
            lines.append(f"Scheduled arrival: {t.scheduled_arrival}")
    elif result.phase == FlightPhase.en_route:
        if t.actual_departure:
            lines.append(f"Departed: {t.actual_departure}")
        if result.relative_position:
            lines.append(result.relative_position)
        if result.position:
            p = result.position
            lines.append(
                f"Position: {p.latitude:.4f}, {p.longitude:.4f}"
                + (f" · {p.ground_speed_kts} kts · hdg {p.heading_deg}°" if p.ground_speed_kts else "")
            )
        if result.remaining_display and t.estimated_arrival:
            lines.append(f"ETA {t.estimated_arrival} (~{result.remaining_display} remaining)")
        elif t.estimated_arrival:
            lines.append(f"ETA {t.estimated_arrival}")
    elif result.phase == FlightPhase.landed:
        if t.actual_arrival:
            lines.append(f"Landed — arrived {t.actual_arrival}")
        elif t.scheduled_arrival:
            lines.append(f"Arrived (scheduled {t.scheduled_arrival})")
    elif result.phase == FlightPhase.cancelled:
        lines.append("Flight cancelled.")
    elif result.phase == FlightPhase.diverted:
        lines.append("Flight diverted.")
    if result.progress_percent is not None and result.phase == FlightPhase.en_route:
        lines.append(f"Progress: {result.progress_percent}%")
    if result.map_latitude is not None and result.map_longitude is not None:
        lines.append(maps_link_block(result.map_latitude, result.map_longitude))
    if result.map_url:
        lines.append(f"FlightAware: {result.map_url}")
    return "\n".join(lines)


async def _aeroapi_get(path: str, *, params: dict | None = None) -> dict:
    key = _api_key()
    if not key:
        raise RuntimeError("FLIGHT_AERO_KEY is not set")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{AEROAPI_BASE}{path}",
            headers={"x-apikey": key},
            params=params or {},
        )
    if resp.status_code == 401:
        raise RuntimeError("FlightAware API key rejected (401)")
    if resp.status_code == 404:
        raise LookupError("Flight not found")
    if resp.status_code == 429:
        raise RuntimeError("FlightAware rate limit exceeded — try again shortly")
    resp.raise_for_status()
    return resp.json()


async def _fetch_flight_leg(ident: str) -> dict:
    today = datetime.now(timezone.utc).date()
    data = await _aeroapi_get(
        f"/flights/{ident}",
        params={
            "start": (today - timedelta(days=1)).isoformat(),
            "end": (today + timedelta(days=2)).isoformat(),
            "max_pages": 1,
        },
    )
    flights = data.get("flights") or []
    leg = _pick_flight(flights)
    if leg is None:
        raise LookupError(f"No flight found for {ident}")
    return leg


async def _fetch_position(fa_flight_id: str) -> dict | None:
    try:
        return await _aeroapi_get(f"/flights/{fa_flight_id}/position")
    except LookupError:
        return None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


def _result_from_leg(
    flight_number: str,
    leg: dict,
    position_payload: dict | None,
) -> FlightStatusResult:
    origin = leg.get("origin") or {}
    dest = leg.get("destination") or {}
    origin_code = _airport_code(origin)
    dest_code = _airport_code(dest)
    route = f"{origin_code or '?'} → {dest_code or '?'}"
    phase = _derive_phase(leg)
    times = _build_times(leg)
    remaining = _remaining_minutes(leg) if phase == FlightPhase.en_route else None

    position: FlightPosition | None = None
    relative: str | None = None
    raw_pos = position_payload
    if position_payload:
        lp = position_payload.get("last_position") or {}
        if lp.get("latitude") is not None and lp.get("longitude") is not None:
            alt_raw = lp.get("altitude")
            alt_ft = int(alt_raw * 100) if alt_raw is not None else None
            position = FlightPosition(
                latitude=float(lp["latitude"]),
                longitude=float(lp["longitude"]),
                altitude_ft=alt_ft,
                ground_speed_kts=lp.get("groundspeed"),
                heading_deg=lp.get("heading"),
                position_time=lp.get("timestamp"),
            )
            dest_coords = _coords(dest_code)
            relative = _describe_position(
                position.latitude,
                position.longitude,
                alt_ft,
                dest_code,
                dest_coords,
            )

    map_pin = _resolve_map_pin(phase, position, origin_code, dest_code)
    map_lat = map_lon = None
    google_maps_url = static_map_image_url = None
    if map_pin:
        map_lat, map_lon, zoom = map_pin
        google_maps_url = maps_link(map_lat, map_lon)
        static_map_image_url = static_map_url(map_lat, map_lon, zoom=zoom)

    result = FlightStatusResult(
        flight_number=flight_number,
        ident=leg.get("ident_icao") or leg.get("ident") or flight_number,
        fa_flight_id=leg.get("fa_flight_id"),
        phase=phase,
        status_detail=leg.get("status"),
        route=route,
        origin_code=origin_code,
        destination_code=dest_code,
        times=times,
        position=position,
        relative_position=relative,
        remaining_minutes=remaining,
        remaining_display=_format_remaining(remaining),
        progress_percent=leg.get("progress_percent"),
        map_url=_map_url(leg),
        map_latitude=map_lat,
        map_longitude=map_lon,
        google_maps_url=google_maps_url,
        static_map_image_url=static_map_image_url,
        raw_flight=leg,
        raw_position=raw_pos,
    )
    result.summary = _build_summary(result)
    return result


def _cache_put(ident: str, result: "FlightStatusResult") -> None:
    """Store result; drop expired entries and cap size (no unbounded growth)."""
    now = time.monotonic()
    for k, (ts, _) in list(_cache.items()):
        if now - ts >= _CACHE_TTL_S:
            _cache.pop(k, None)
    while len(_cache) >= _CACHE_MAX:
        oldest = min(_cache.items(), key=lambda kv: kv[1][0])
        _cache.pop(oldest[0], None)
    _cache[ident] = (now, result)


async def track_flight(flight_number: str) -> FlightStatusResult:
    """Look up live or recent status for a commercial flight ident."""
    ident = _normalize_ident(flight_number)
    if not ident:
        raise ValueError("flight_number is required")

    cached = _cache.get(ident)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_S:
        return cached[1]

    existing = _inflight.get(ident)
    if existing is not None and not existing.done():
        return await asyncio.shield(existing)

    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _inflight[ident] = fut
    try:
        leg = await _fetch_flight_leg(ident)
        phase = _derive_phase(leg)
        pos_payload = None
        if phase == FlightPhase.en_route and leg.get("fa_flight_id"):
            pos_payload = await _fetch_position(leg["fa_flight_id"])
        result = _result_from_leg(ident, leg, pos_payload)
        _cache_put(ident, result)
        if not fut.done():
            fut.set_result(result)
        return result
    except BaseException as exc:
        # Must resolve waiters on CancelledError too (not a subclass of Exception).
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        if _inflight.get(ident) is fut:
            _inflight.pop(ident, None)


def flight_status_to_json(result: FlightStatusResult) -> str:
    """Tool-friendly JSON: summary + structured core + raw API payloads."""
    import json

    payload = {
        "summary": result.summary,
        "core": result.model_dump(
            exclude={"summary", "raw_flight", "raw_position"},
            mode="json",
        ),
        "raw": {
            "flight": result.raw_flight,
            "position": result.raw_position,
        },
    }
    return json.dumps(payload, indent=2)


def clear_flight_cache_for_tests() -> None:
    _cache.clear()
    _inflight.clear()
