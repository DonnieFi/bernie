"""Halifax Transit live vehicle tools."""
from __future__ import annotations

from http_session import get_http_session

from tools import ROLE_ALL, tool
from transit_service import (
    fetch_vehicles,
    filter_route,
    format_proximity,
    format_route_list,
    format_track_snapshot,
    nearest_vehicle,
    normalize_route_id,
    refresh_zones,
    resolve_landmark,
)
from transit_tracking import tracking_manager


@tool(
    name="get_route_buses",
    description=(
        "List all active Halifax Transit vehicles on a route with map links. "
        "Use for 'show me all the number 4 buses' or 'which buses are on route 1'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "route_id": {
                "type": "string",
                "description": "Bus route number (required, e.g. '4').",
            },
        },
        "required": ["route_id"],
    },
    role_required=ROLE_ALL,
    domain="transit",
    tier=1,
)
async def handle_get_route_buses(args: dict, ctx) -> str:
    route_id = args.get("route_id", "").strip()
    if not route_id:
        return "route_id is required."
    try:
        session = get_http_session()
        vehicles = await fetch_vehicles(session)
        return format_route_list(vehicles, route_id)
    except Exception as e:
        return f"Halifax Transit feed unavailable: {e}"


@tool(
    name="get_bus_proximity",
    description=(
        "Nearest active bus on a route to a landmark (home, sacredheart, school) "
        "or caller GPS. Use for 'is there a number 4 near Sacred Heart' or "
        "'any route 4 buses near me'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "route_id": {
                "type": "string",
                "description": "Bus route number (required).",
            },
            "landmark": {
                "type": "string",
                "description": (
                    "Target: home, sacred_heart, school, caller (asker's GPS), "
                    "or any HA zone slug (e.g. sacredheart)."
                ),
            },
        },
        "required": ["route_id", "landmark"],
    },
    role_required=ROLE_ALL,
    domain="transit",
    tier=1,
)
async def handle_get_bus_proximity(args: dict, ctx) -> str:
    route_id = args.get("route_id", "").strip()
    landmark = args.get("landmark", "home").strip()
    if not route_id:
        return "route_id is required."
    try:
        session = get_http_session()
        await refresh_zones()
        target = await resolve_landmark(
            landmark, person_id=ctx.person_id, session=session
        )
        if isinstance(target, str):
            return target
        vehicles = filter_route(await fetch_vehicles(session), route_id)
        bus, dist = nearest_vehicle(vehicles, target)
        if not bus or dist is None:
            return f"No active vehicles on route {normalize_route_id(route_id)}."
        return format_proximity(bus, dist, target)
    except Exception as e:
        return f"Halifax Transit feed unavailable: {e}"


@tool(
    name="track_vehicle",
    description=(
        "Live snapshot of one Halifax Transit bus by vehicle ID and route number "
        "(position, speed, map link, distance to a landmark). Background polling "
        "and #smithy home announcements require `/bus track` in Discord — this "
        "tool does not start a tracking session."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "vehicle_id": {
                "type": "string",
                "description": "Vehicle ID from the feed (e.g. '3160').",
            },
            "route_id": {
                "type": "string",
                "description": "Route number for validation (required).",
            },
            "landmark": {
                "type": "string",
                "description": "Distance target landmark (default home).",
                "default": "home",
            },
            "bind_for_person": {
                "type": "string",
                "description": (
                    "Optional: remember this vehicle for a family member (e.g. child1) "
                    "for follow-up questions. Omit for a one-off snapshot."
                ),
            },
        },
        "required": ["vehicle_id", "route_id"],
    },
    role_required=ROLE_ALL,
    domain="transit",
    tier=1,
)
async def handle_track_vehicle(args: dict, ctx) -> str:
    vehicle_id = str(args.get("vehicle_id", "")).strip()
    route_id = args.get("route_id", "").strip()
    landmark = args.get("landmark", "home").strip()
    if not vehicle_id or not route_id:
        return "vehicle_id and route_id are required."

    bind_pid = (args.get("bind_for_person") or "").strip()
    if bind_pid:
        tracking_manager.set_vehicle_binding(bind_pid, vehicle_id, route_id)

    try:
        session = get_http_session()
        await refresh_zones()
        target = await resolve_landmark(
            landmark, person_id=ctx.person_id, session=session
        )
        if isinstance(target, str):
            return target
        vehicles = await fetch_vehicles(session)
        bus = next((v for v in vehicles if v.vehicle_id == vehicle_id), None)
        if not bus:
            return f"Vehicle {vehicle_id} not in live feed."
        from transit_service import haversine_m

        dist = haversine_m(bus.lat, bus.lon, target.lat, target.lon)
        return format_track_snapshot(bus, target, dist)
    except Exception as e:
        return f"Halifax Transit feed unavailable: {e}"


# ── bus stop parity (slash /bus stop) ─────────────────────────────────────────
@tool(
    name="stop_bus_tracking",
    description="Stop your (or specified) active Halifax Transit tracking session (equivalent to /bus stop). Pass user_id (discord snowflake) when known from context.",
    input_schema={
        "type": "object",
        "properties": {
            "user_id": {"type": ["string", "integer"], "description": "Discord user ID (int or str) for the tracking session owner."},
        },
        "required": [],
    },
    role_required=ROLE_ALL,
    domain="transit",
    tier=1,
)
async def handle_stop_bus_tracking(args: dict, ctx) -> str:
    if ctx.shadow:
        return "[shadow: would stop bus tracking]"
    from transit_tracking import tracking_manager
    from task_access import person_to_discord_id
    uid = args.get("user_id")
    if uid is None and getattr(ctx, "person_id", None):
        # Auto-resolve using decoupled person_to_discord_id (handles 'person:xxx', aliases etc.)
        did = person_to_discord_id(ctx.person_id)
        if did:
            uid = did
    if uid is None:
        return "No active session stopped (provide user_id or ensure person has discord_id; or use /bus stop in Discord)."
    try:
        uid_int = int(uid)
        stopped = await tracking_manager.stop_session(uid_int, reason="tool-requested stop")
        if stopped:
            return "Bus tracking session stopped."
        return f"No active tracking session found for user_id {uid_int}."
    except Exception as e:
        return f"Stop failed: {e}"
