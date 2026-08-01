"""Flight status tools — FlightAware AeroAPI."""
from __future__ import annotations

from flight_service import flight_status_to_json, track_flight
from tools import ROLE_ALL, tool


@tool(
    name="get_flight_status",
    description=(
        "Track a commercial flight by flight number (e.g. AC123, OCN74, 4Y74). "
        "Returns schedule, delays, en-route position (lat/lon, altitude, speed), "
        "ETA, Google Maps + static map image when position is available, and a "
        "FlightAware track link. Use for 'where is flight X' or "
        "'when does flight X land'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "flight_number": {
                "type": "string",
                "description": "Airline flight number or ident (e.g. AC847, OCN74, 4Y74).",
            },
        },
        "required": ["flight_number"],
    },
    role_required=ROLE_ALL,
    domain="flights",
    tier=1,
)
async def handle_get_flight_status(args: dict, ctx) -> str:
    flight_number = (args.get("flight_number") or "").strip()
    if not flight_number:
        return "flight_number is required."
    try:
        result = await track_flight(flight_number)
        return flight_status_to_json(result)
    except ValueError as exc:
        return str(exc)
    except LookupError:
        return f"No flight found for {flight_number.upper()} in the current window."
    except RuntimeError as exc:
        return f"FlightAware unavailable: {exc}"
    except Exception as exc:
        return f"Flight status lookup failed: {exc}"
