"""Presence / location / battery tool handlers."""
from __future__ import annotations

from datetime import datetime, timezone

from tools import ROLE_ALL, tool


@tool(
    name="who_is_home",
    description=(
        "Quick check of who is home vs away right now. Hits Unifi + Google "
        "WiFi + HA live. Use get_person_location instead when GPS or a map "
        "link is needed."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_who_is_home(args: dict, ctx) -> str:
    from presence_service import presence_service
    from constants import registry as person_registry

    full = await presence_service.get_full_presence()
    if not full:
        return "Presence data is currently unavailable."
    lines = []
    for pid, loc in full.items():
        person = person_registry.get(pid)
        # Friends (role: "friend" in config) are deliberately excluded from family presence answers
        if person and person.get("role") == "friend":
            continue
        name = loc.get("display") or loc.get("name", pid).capitalize()
        status = loc.get("status_label", "away")
        lines.append(f"• {name} is {status}")
    return "\n".join(lines)


@tool(
    name="get_person_location",
    description=(
        "Get location for a family member: live Unifi + HA network presence, "
        "HA zone, GPS coordinates, and a Google Maps link. Always includes a "
        "Google Maps link when GPS is available. Does NOT include battery. "
        "Pass 'all' for everyone."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "person": {
                "type": "string",
                "description": "Person's role name ('Dad', 'Mom', 'Child1', 'Child2') or real name, or 'all'.",
            },
        },
        "required": ["person"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_person_location(args: dict, ctx) -> str:
    from presence_service import presence_service
    from constants import registry as person_registry

    person_arg = args.get("person", "").strip()
    full = await presence_service.get_full_presence()

    if person_arg.lower() == "all":
        family_members = ctx.config.get("family_members", {})
        family_pids = {
            (m.get("canonical_id") or display).lower()
            for display, m in family_members.items()
            if m.get("role") != "friend"
        }
        locations = [v for k, v in full.items() if k in family_pids]
    else:
        pid = person_registry.resolve(person_arg) or person_arg.lower()
        loc = full.get(pid)
        locations = [loc] if loc else []

    if not locations:
        return f"No location data for {person_arg}."

    lines = []
    for loc in locations:
        gps = loc.get("gps")
        name = loc.get("display") or loc.get("name", "?")
        loc_str = loc.get("status_label", "away")
        if gps and gps.get("lat"):
            acc = gps.get("accuracy")
            acc_str = f" ±{acc}m" if acc else ""
            gps_updated = loc.get("gps_updated")
            age_str = ""
            if gps_updated:
                try:
                    updated = datetime.fromisoformat(gps_updated.replace("Z", "+00:00"))
                    age_mins = int((datetime.now(timezone.utc) - updated).total_seconds() / 60)
                    if age_mins < 2:
                        age_str = " · just now"
                    elif age_mins < 60:
                        age_str = f" · {age_mins}m old"
                    else:
                        age_str = f" · {age_mins // 60}h{age_mins % 60}m old"
                except Exception:
                    pass
            loc_str += f" · [Open in Maps](https://maps.google.com/?q={gps['lat']},{gps['lon']}){acc_str}{age_str}"
        else:
            loc_str += " · GPS unavailable"
        lines.append(f"**{name}**: {loc_str}")
    return "\n".join(lines)


@tool(
    name="get_battery",
    description=(
        "Get phone battery level for a family member or everyone. Use only "
        "when battery is specifically asked about."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "Person's role/real name, or 'all'."},
        },
        "required": ["person"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_battery(args: dict, ctx) -> str:
    from presence_service import presence_service
    from constants import registry as person_registry

    person_arg = args.get("person", "").strip()
    full = await presence_service.get_full_presence()

    if person_arg.lower() == "all":
        family_members = ctx.config.get("family_members", {})
        family_pids = {
            (m.get("canonical_id") or display).lower()
            for display, m in family_members.items()
            if m.get("role") != "friend"
        }
        locations = [v for k, v in full.items() if k in family_pids]
    else:
        pid = person_registry.resolve(person_arg) or person_arg.lower()
        loc = full.get(pid)
        locations = [loc] if loc else []

    if not locations:
        return f"No data for {person_arg}."

    lines = []
    for loc in locations:
        name = loc.get("display") or loc.get("name", "?")
        batt = loc.get("battery")
        lines.append(f"**{name}**: {batt}%" if batt is not None else f"**{name}**: unknown")
    return "\n".join(lines)
