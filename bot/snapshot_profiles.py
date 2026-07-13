"""Entity ID maps and fetch logic for vehicle/sleep snapshots.

xcq.6 — Prefer config.json ``snapshot_profiles`` so the public tree can ship
empty maps. Built-in defaults remain as private-deploy fallback when config
omits the block (so existing homes keep working without a config migration).
"""
from __future__ import annotations

from typing import Any

from snapshot_models import (
    SleepCore,
    SleepExtras,
    SleepSummary,
    VehicleCore,
    VehicleExtras,
    VehicleStatus,
)

MILES_TO_KM = 1.60934

# Built-in fallbacks (private installs). Public examples use empty maps in config.
_BUILTIN_VEHICLE_PROFILES: dict[str, dict[str, dict[str, str]]] = {
    "nirochan": {
        "core": {
            "lock": "lock.nirochan_door_lock",
            "ev_battery_pct": "sensor.nirochan_ev_battery_level",
            "plugged_in": "binary_sensor.nirochan_ev_battery_plug",
            "charging": "switch.nirochan_ev_charging",
            "location": "device_tracker.nirochan_location",
            "last_updated": "sensor.nirochan_last_updated_at",
        },
        "extras": {
            "ev_range_km": "sensor.nirochan_ev_range",
            "total_range_km": "sensor.nirochan_total_driving_range",
            "odometer": "sensor.nirochan_odometer",
            "climate_on": "switch.nirochan_climate",
            "fuel_pct": "sensor.nirochan_fuel_level",
            "battery_12v_pct": "sensor.nirochan_car_battery_level",
        },
    },
}

_BUILTIN_SLEEP_PROFILES: dict[tuple[str, str], dict[str, dict[str, str]]] = {
    ("dad", "garmin"): {
        "core": {
            "sleep_score": "sensor.garmin_connect_sleep_score",
            "total_sleep": "sensor.garmin_connect_total_sleep_duration",
            "hrv": "sensor.garmin_connect_hrv_last_night_average",
            "resting_hr": "sensor.garmin_connect_resting_heart_rate",
            "deep_sleep": "sensor.garmin_connect_deep_sleep",
            "rem_sleep": "sensor.garmin_connect_rem_sleep",
            "light_sleep": "sensor.garmin_connect_light_sleep",
        },
        "extras": {
            "body_battery": "sensor.garmin_connect_body_battery",
            "stress_avg": "sensor.garmin_connect_average_stress_level",
            "hrv_weekly": "sensor.garmin_connect_hrv_weekly_average",
            "hrv_status": "sensor.garmin_connect_hrv_status",
        },
    },
}


def _config_snapshot_block() -> dict[str, Any]:
    try:
        from config import config as _cfg
        block = _cfg.get("snapshot_profiles")
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


def vehicle_profiles() -> dict[str, dict[str, dict[str, str]]]:
    """Resolved vehicle entity maps.

    - ``snapshot_profiles`` missing → private-deploy builtins
    - ``vehicles`` key present (even ``{}``) → config wins (OSS empty maps stay empty)
    """
    block = _config_snapshot_block()
    if "vehicles" in block:
        custom = block.get("vehicles")
        if not isinstance(custom, dict):
            return {}
        return {str(k).lower(): v for k, v in custom.items() if isinstance(v, dict)}
    return _BUILTIN_VEHICLE_PROFILES


def sleep_profiles() -> dict[tuple[str, str], dict[str, dict[str, str]]]:
    """Resolved sleep maps. Config form: {\"person|source\": {core, extras}} or nested.

    Same empty-vs-missing semantics as vehicle_profiles.
    """
    block = _config_snapshot_block()
    if "sleep" not in block:
        return _BUILTIN_SLEEP_PROFILES
    custom = block.get("sleep")
    if not isinstance(custom, dict) or not custom:
        return {}
    out: dict[tuple[str, str], dict[str, dict[str, str]]] = {}
    for key, profile in custom.items():
        if not isinstance(profile, dict):
            continue
        if isinstance(key, str) and "|" in key:
            person, source = key.split("|", 1)
            out[(person.lower(), source.lower())] = profile
        elif isinstance(key, str) and isinstance(profile.get("source"), str):
            out[(key.lower(), str(profile["source"]).lower())] = profile
        else:
            for source, sub in profile.items():
                if isinstance(sub, dict) and "core" in sub:
                    out[(str(key).lower(), str(source).lower())] = sub
    return out


# Back-compat: tests import VEHICLE_PROFILES / SLEEP_PROFILES as mapping-like objects.
class _ProfilesProxy(dict):
    """dict-like that re-resolves from config each lookup."""

    def __init__(self, resolver):
        super().__init__()
        self._resolver = resolver

    def _data(self):
        return self._resolver()

    def __getitem__(self, key):
        return self._data()[key]

    def get(self, key, default=None):
        return self._data().get(key, default)

    def __contains__(self, key):
        return key in self._data()

    def keys(self):
        return self._data().keys()

    def values(self):
        return self._data().values()

    def items(self):
        return self._data().items()

    def __iter__(self):
        return iter(self._data())

    def __len__(self):
        return len(self._data())


VEHICLE_PROFILES = _ProfilesProxy(vehicle_profiles)
SLEEP_PROFILES = _ProfilesProxy(sleep_profiles)

_DURATION_FIELDS = frozenset({"total_sleep", "deep_sleep", "rem_sleep", "light_sleep"})


def format_duration_minutes(minutes: float | int | None) -> str | None:
    if minutes is None:
        return None
    try:
        total = int(round(float(minutes)))
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value: Any) -> int | None:
    parsed = _parse_float(value)
    if parsed is None:
        return None
    return int(round(parsed))


def _parse_on_off(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).lower()
    if text in {"on", "true", "yes", "1"}:
        return True
    if text in {"off", "false", "no", "0", "unavailable", "unknown"}:
        return False if text in {"off", "false", "no", "0"} else None
    return None


async def _fetch_state(ha_service, entity_id: str) -> dict | None:
    state = await ha_service.get_state(entity_id)
    if not state or not state.get("entity_id"):
        return None
    return state


def _distance_to_km(state: dict | None) -> float | None:
    if not state:
        return None
    value = _parse_float(state.get("state"))
    if value is None:
        return None
    unit = str(state.get("attributes", {}).get("unit_of_measurement", "")).lower()
    if unit in {"mi", "mile", "miles"}:
        return round(value * MILES_TO_KM, 1)
    return value


def _field_from_state(field: str, state: dict | None) -> Any:
    if not state:
        return None
    raw = state.get("state")
    if field in _DURATION_FIELDS:
        return format_duration_minutes(_parse_float(raw))
    if field in {"plugged_in", "charging", "climate_on"}:
        return _parse_on_off(raw)
    if field == "lock":
        return str(raw).lower() if raw not in {None, "unknown", "unavailable"} else None
    if field == "location":
        return str(raw) if raw not in {None, "unknown", "unavailable"} else None
    if field == "last_updated":
        return str(raw) if raw not in {None, "unknown", "unavailable"} else None
    if field == "hrv_status":
        return str(raw) if raw not in {None, "unknown", "unavailable"} else None
    if field in {"ev_range_km", "total_range_km"}:
        return _distance_to_km(state)
    if field == "sleep_score":
        return _parse_int(raw)
    if field == "body_battery":
        return _parse_int(raw)
    return _parse_float(raw)


async def _fetch_fields(
    ha_service,
    mapping: dict[str, str],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, entity_id in mapping.items():
        state = await _fetch_state(ha_service, entity_id)
        out[field] = _field_from_state(field, state)
    return out


def vehicle_summary(status: VehicleStatus) -> str:
    c = status.core
    parts: list[str] = [status.vehicle.capitalize()]
    if c.lock:
        parts.append(c.lock)
    if c.ev_battery_pct is not None:
        pct = int(c.ev_battery_pct) if c.ev_battery_pct == int(c.ev_battery_pct) else c.ev_battery_pct
        parts.append(f"{pct}% EV")
    if c.plugged_in is True:
        parts.append("plugged in")
    elif c.plugged_in is False:
        parts.append("unplugged")
    if c.charging is True:
        parts.append("charging")
    if c.location:
        parts.append(f"at {c.location}")
    return ": ".join([parts[0], ", ".join(parts[1:])]) if len(parts) > 1 else parts[0]


def sleep_summary_line(summary: SleepSummary) -> str:
    c = summary.core
    parts: list[str] = [summary.person.capitalize()]
    if c.sleep_score is not None:
        parts.append(f"score {c.sleep_score}")
    if c.total_sleep:
        parts.append(c.total_sleep)
    if c.hrv is not None:
        parts.append(f"HRV {c.hrv:g}")
    if c.resting_hr is not None:
        parts.append(f"RHR {c.resting_hr:g}")
    return ": ".join([parts[0], ", ".join(parts[1:])]) if len(parts) > 1 else parts[0]


async def fetch_vehicle_status(
    ha_service,
    vehicle: str,
    *,
    extras: bool = True,
) -> VehicleStatus | None:
    profile = vehicle_profiles().get(vehicle.lower())
    if not profile:
        return None
    core_data = await _fetch_fields(ha_service, profile["core"])
    core = VehicleCore(**core_data)
    extra_model = None
    if extras:
        extra_data = await _fetch_fields(ha_service, profile["extras"])
        extra_model = VehicleExtras(**extra_data)
    return VehicleStatus(vehicle=vehicle.lower(), core=core, extras=extra_model)


async def fetch_sleep_summary(
    ha_service,
    person: str,
    *,
    source: str = "garmin",
    extras: bool = True,
) -> SleepSummary | None:
    profile = sleep_profiles().get((person.lower(), source.lower()))
    if not profile:
        return None
    core_data = await _fetch_fields(ha_service, profile["core"])
    core = SleepCore(**core_data)
    extra_model = None
    if extras:
        extra_data = await _fetch_fields(ha_service, profile["extras"])
        extra_model = SleepExtras(**extra_data)
    return SleepSummary(person=person.lower(), source=source.lower(), core=core, extras=extra_model)
