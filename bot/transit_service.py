"""Halifax Transit GTFS-RT vehicle positions + HA zone landmarks (Phase 14)."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from config import config

log = logging.getLogger(__name__)

_FEED_CACHE: tuple[float, list[VehicleSnapshot]] | None = None
_FEED_INFLIGHT: asyncio.Future | None = None
_ZONES: dict[str, ZoneLandmark] = {}
_ZONES_FETCHED_AT: float = 0.0

_DEFAULT_FEED = "https://gtfs.halifax.ca/realtime/Vehicle/VehiclePositions.pb"
_CARDINALS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


@dataclass(frozen=True)
class VehicleSnapshot:
    vehicle_id: str
    route_id: str
    lat: float
    lon: float
    bearing: float | None
    speed_kmh: float | None
    feed_timestamp: float | None


@dataclass(frozen=True)
class ZoneLandmark:
    entity_id: str
    slug: str
    label: str
    lat: float
    lon: float
    radius_m: float


@dataclass(frozen=True)
class LatLon:
    lat: float
    lon: float
    label: str
    radius_m: float | None = None


def _transit_cfg() -> dict:
    return config.get("transit") or {}


def _feed_url() -> str:
    return _transit_cfg().get("feed_url") or _DEFAULT_FEED


def _feed_cache_seconds() -> int:
    return int(_transit_cfg().get("cache_seconds", 15))


def _zone_cache_seconds() -> float:
    days = float(_transit_cfg().get("zone_cache_days", 7))
    return days * 86400


def normalize_route_id(route_id: str) -> str:
    r = (route_id or "").strip()
    if r.isdigit():
        return str(int(r))
    return r


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def maps_link(lat: float, lon: float) -> str:
    return f"https://maps.google.com/?q={lat:.5f},{lon:.5f}"


def static_map_url(lat: float, lon: float, *, zoom: int = 15) -> str:
    """OSM static map image — Discord embeds this reliably (no API key)."""
    return (
        "https://staticmap.openstreetmap.de/staticmap.php?"
        f"center={lat:.5f},{lon:.5f}&zoom={zoom}&size=400x200"
        f"&markers={lat:.5f},{lon:.5f},red-pushpin"
    )


def maps_link_block(lat: float, lon: float) -> str:
    """Markdown link + bare URL (bare line helps some clients unfurl Maps)."""
    url = maps_link(lat, lon)
    return f"📍 [Open in Maps]({url})\n{url}"


def _bearing_cardinal(degrees: float | None) -> str:
    if degrees is None:
        return "—"
    idx = int((degrees + 22.5) / 45) % 8
    return _CARDINALS[idx]


def _speed_kmh(raw_speed: float | None) -> float | None:
    """GTFS-RT position.speed is m/s."""
    if raw_speed is None:
        return None
    return raw_speed * 3.6


def _parse_feed_bytes(data: bytes) -> list[VehicleSnapshot]:
    from google.transit import gtfs_realtime_pb2

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(data)
    feed_ts = float(feed.header.timestamp) if feed.header.timestamp else None
    out: list[VehicleSnapshot] = []
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        if not v.HasField("position"):
            continue
        pos = v.position
        route = ""
        if v.HasField("trip") and v.trip.route_id:
            route = normalize_route_id(v.trip.route_id)
        vid = v.vehicle.id if v.HasField("vehicle") and v.vehicle.id else entity.id
        bearing = pos.bearing if pos.HasField("bearing") else None
        speed = _speed_kmh(pos.speed if pos.HasField("speed") else None)
        out.append(
            VehicleSnapshot(
                vehicle_id=str(vid),
                route_id=route,
                lat=pos.latitude,
                lon=pos.longitude,
                bearing=bearing,
                speed_kmh=speed,
                feed_timestamp=feed_ts,
            )
        )
    return out


async def fetch_vehicles(session: aiohttp.ClientSession | None = None) -> list[VehicleSnapshot]:
    """Fetch VehiclePositions feed with in-memory TTL cache + single-flight (1bf.6)."""
    global _FEED_CACHE, _FEED_INFLIGHT
    now = time.monotonic()
    ttl = _feed_cache_seconds()
    if _FEED_CACHE and (now - _FEED_CACHE[0]) < ttl:
        return _FEED_CACHE[1]

    if _FEED_INFLIGHT is not None and not _FEED_INFLIGHT.done():
        return await asyncio.shield(_FEED_INFLIGHT)

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _FEED_INFLIGHT = fut
    try:
        vehicles = await _fetch_vehicles_uncached(session)
        _FEED_CACHE = (time.monotonic(), vehicles)
        if not fut.done():
            fut.set_result(vehicles)
        return vehicles
    except Exception as e:
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        if _FEED_INFLIGHT is fut:
            _FEED_INFLIGHT = None


async def _fetch_vehicles_uncached(session: aiohttp.ClientSession | None = None) -> list[VehicleSnapshot]:
    url = _feed_url()
    close_session = False
    if session is None:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
        close_session = True
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Transit feed HTTP {resp.status}")
            data = await resp.read()
        return _parse_feed_bytes(data)
    finally:
        if close_session:
            await session.close()


def invalidate_feed_cache() -> None:
    global _FEED_CACHE
    _FEED_CACHE = None


async def refresh_zones(*, force: bool = False) -> dict[str, ZoneLandmark]:
    """Load all zone.* entities from Home Assistant."""
    global _ZONES, _ZONES_FETCHED_AT
    now = time.monotonic()
    if not force and _ZONES and (now - _ZONES_FETCHED_AT) < _zone_cache_seconds():
        return _ZONES

    from ha_service import ha_service

    states = await ha_service.get_live_states(domain="zone")
    zones: dict[str, ZoneLandmark] = {}
    for s in states:
        eid = s.get("entity_id", "")
        if not eid.startswith("zone."):
            continue
        attrs = s.get("attributes") or {}
        lat = attrs.get("latitude")
        lon = attrs.get("longitude")
        if lat is None or lon is None:
            continue
        slug = eid.split(".", 1)[1]
        label = attrs.get("friendly_name") or slug.replace("_", " ").title()
        radius = float(attrs.get("radius") or 100.0)
        zm = ZoneLandmark(
            entity_id=eid,
            slug=slug,
            label=str(label),
            lat=float(lat),
            lon=float(lon),
            radius_m=radius,
        )
        zones[eid] = zm
        zones[slug] = zm
        zones[slug.lower()] = zm

    aliases = _transit_cfg().get("landmark_aliases") or {}
    for alias, target in aliases.items():
        key = target.replace("zone.", "") if target.startswith("zone.") else target
        if key in zones:
            zones[alias.lower()] = zones[key]

    _ZONES = zones
    _ZONES_FETCHED_AT = now
    log.info("Transit zone cache refreshed — %d HA zones", len({z.entity_id for z in zones.values()}))
    return _ZONES


def zones_cache_age_seconds() -> float:
    """Seconds since zones were loaded; -1 if never."""
    if not _ZONES_FETCHED_AT:
        return -1.0
    return time.monotonic() - _ZONES_FETCHED_AT


def list_landmark_choices() -> list[str]:
    """Slugs for slash autocomplete (unique zone slugs + aliases + caller)."""
    choices = ["caller", "home"]
    seen: set[str] = set()
    for key, z in _ZONES.items():
        if "." in key:
            continue
        if z.slug in seen:
            continue
        seen.add(z.slug)
        choices.append(z.slug)
    for alias in (_transit_cfg().get("landmark_aliases") or {}):
        if alias not in seen:
            choices.append(alias)
    return sorted(set(choices), key=str.lower)


async def resolve_landmark(
    landmark: str,
    *,
    person_id: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> LatLon | str:
    """Return LatLon or an error string."""
    await refresh_zones()
    key = (landmark or "home").strip().lower().replace(" ", "_")

    if key in ("caller", "me"):
        if not person_id:
            return "No caller identity — specify a landmark like home or sacredheart."
        from presence_service import presence_service

        full = await presence_service.get_full_presence()
        loc = full.get(person_id.lower()) or full.get(person_id)
        if not loc:
            return f"No presence data for {person_id}."
        gps = loc.get("gps")
        if not gps or gps.get("lat") is None:
            return "No live GPS for you — try landmark home or sacredheart."
        return LatLon(
            lat=float(gps["lat"]),
            lon=float(gps["lon"]),
            label=f"{loc.get('display', person_id)} (live GPS)",
            radius_m=150.0,
        )

    z = _ZONES.get(key) or _ZONES.get(f"zone.{key}")
    if z:
        return LatLon(lat=z.lat, lon=z.lon, label=z.label, radius_m=z.radius_m)
    return f"Unknown landmark {landmark!r}. Try: {', '.join(list_landmark_choices()[:12])}…"


def filter_route(vehicles: list[VehicleSnapshot], route_id: str) -> list[VehicleSnapshot]:
    want = normalize_route_id(route_id)
    return [v for v in vehicles if v.route_id == want]


def nearest_vehicle(
    vehicles: list[VehicleSnapshot], target: LatLon
) -> tuple[VehicleSnapshot | None, float | None]:
    if not vehicles:
        return None, None
    best_v: VehicleSnapshot | None = None
    best_d = float("inf")
    for v in vehicles:
        d = haversine_m(v.lat, v.lon, target.lat, target.lon)
        if d < best_d:
            best_d = d
            best_v = v
    return best_v, best_d if best_v else None


def format_vehicle_line(v: VehicleSnapshot, *, prefix: str = "") -> str:
    if v.speed_kmh is not None and v.speed_kmh < 1.0:
        motion = "Stopped"
    elif v.speed_kmh is not None:
        motion = f"Moving {_bearing_cardinal(v.bearing)} at {v.speed_kmh:.0f} km/h"
    else:
        motion = f"Heading {_bearing_cardinal(v.bearing)}"
    route = f"Route {v.route_id} · " if v.route_id else ""
    return f"{prefix}**Bus {v.vehicle_id}:** {route}{motion}\n{maps_link_block(v.lat, v.lon)}"


def format_route_list(vehicles: list[VehicleSnapshot], route_id: str) -> str:
    route = normalize_route_id(route_id)
    matched = filter_route(vehicles, route)
    if not matched:
        return f"No active vehicles on route {route} in the live feed."
    lines = [f"**🚌 Active route {route} buses ({len(matched)}):**"]
    for v in sorted(matched, key=lambda x: x.vehicle_id):
        lines.append(f"• {format_vehicle_line(v)}")
    return "\n".join(lines)


def format_proximity(
    v: VehicleSnapshot,
    distance_m: float,
    target: LatLon,
    *,
    trend: str | None = None,
) -> str:
    trend_line = f"\n- **Trend:** {trend}" if trend else ""
    speed = f"{v.speed_kmh:.0f} km/h" if v.speed_kmh is not None else "—"
    return (
        f"**🚌 Nearest route {v.route_id} bus to {target.label}:**\n"
        f"- **Vehicle ID:** `{v.vehicle_id}`\n"
        f"- **Distance:** ~{distance_m:.0f}m straight-line{trend_line}\n"
        f"- **Status:** {_bearing_cardinal(v.bearing)} at {speed}\n"
        f"{maps_link_block(v.lat, v.lon)}"
    )


def format_track_snapshot(
    v: VehicleSnapshot,
    target: LatLon,
    distance_m: float,
    *,
    tick: int | None = None,
    trend: str | None = None,
) -> str:
    header = f"**📍 Tracking bus {v.vehicle_id} (route {v.route_id})**"
    if tick is not None:
        header += f" — update {tick}"
    trend_line = f"\n- **Trend:** {trend}" if trend else ""
    speed = f"{v.speed_kmh:.0f} km/h" if v.speed_kmh is not None else "—"
    return (
        f"{header}\n"
        f"- **Target:** {target.label}\n"
        f"- **Distance:** ~{distance_m:.0f}m straight-line{trend_line}\n"
        f"- **Status:** {_bearing_cardinal(v.bearing)} at {speed}\n"
        f"{maps_link_block(v.lat, v.lon)}"
    )


async def get_person_home_state(person_id: str) -> str | None:
    """Return HA person entity state (e.g. home, sacredheart) or None."""
    trackers = config.get("presence", {}).get("device_trackers", {})
    cfg = trackers.get(person_id.lower())
    if not cfg:
        return None
    entity = cfg.get("person_entity")
    if not entity:
        return None
    from ha_service import ha_service

    state = await ha_service.get_state(entity)
    return state.get("state")


def clear_zone_cache_for_tests() -> None:
    global _ZONES, _ZONES_FETCHED_AT, _FEED_CACHE
    _ZONES = {}
    _ZONES_FETCHED_AT = 0.0
    _FEED_CACHE = None
