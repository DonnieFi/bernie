"""Structured vehicle and sleep snapshot tools."""
from __future__ import annotations

import json

from snapshot_profiles import (
    fetch_sleep_summary,
    fetch_vehicle_status,
    sleep_summary_line,
    vehicle_summary,
)
from tools import ROLE_ALL, tool


def _ha(ctx):
    """Prefer the injected HA service; fall back to the module-level singleton."""
    svc = getattr(ctx.services, "ha", None)
    if svc is not None:
        return svc
    from ha_service import ha_service
    return ha_service


def _snapshot_payload(summary_line: str, core, extras) -> str:
    payload = {
        "summary": summary_line,
        "core": core.model_dump(),
        "extras": extras.model_dump() if extras is not None else None,
    }
    return json.dumps(payload, indent=2)


@tool(
    name="get_vehicle_status",
    description=(
        "Get a structured snapshot of a family vehicle (lock, EV battery, plug/charge, "
        "location). Uses curated entity maps — lock status comes from the lock entity only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "vehicle": {
                "type": "string",
                "description": "Vehicle profile key (default: nirochan).",
            },
            "extras": {
                "type": "boolean",
                "description": "Include range, odometer, climate, fuel, and 12V battery.",
            },
        },
        "required": [],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_vehicle_status(args: dict, ctx) -> str:
    vehicle = (args.get("vehicle") or "nirochan").lower()
    include_extras = args.get("extras", True)
    status = await fetch_vehicle_status(_ha(ctx), vehicle, extras=include_extras)
    if status is None:
        return f"Unknown vehicle profile '{vehicle}'."
    return _snapshot_payload(vehicle_summary(status), status.core, status.extras)


@tool(
    name="get_sleep_summary",
    description=(
        "Get a structured sleep snapshot for a family member from Garmin Connect sensors "
        "(score, duration, stages, HRV, resting HR)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "person": {
                "type": "string",
                "description": "Person profile key (defaults to the caller).",
            },
            "source": {
                "type": "string",
                "description": "Data source profile (default: garmin).",
            },
            "extras": {
                "type": "boolean",
                "description": "Include body battery, stress, and weekly HRV fields.",
            },
        },
        "required": [],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_sleep_summary(args: dict, ctx) -> str:
    person = args.get("person") or ctx.person_id
    if not person:
        return "No person specified and caller identity is unknown."
    source = (args.get("source") or "garmin").lower()
    include_extras = args.get("extras", True)
    summary = await fetch_sleep_summary(
        _ha(ctx),
        person,
        source=source,
        extras=include_extras,
    )
    if summary is None:
        return f"No sleep profile for person='{person}' source='{source}'."
    return _snapshot_payload(sleep_summary_line(summary), summary.core, summary.extras)
