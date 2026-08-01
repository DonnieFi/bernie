"""Pydantic models for structured vehicle and sleep snapshots."""
from __future__ import annotations

from pydantic import BaseModel, Field


class VehicleCore(BaseModel):
    lock: str | None = None
    ev_battery_pct: float | None = None
    plugged_in: bool | None = None
    charging: bool | None = None
    location: str | None = None
    last_updated: str | None = None


class VehicleExtras(BaseModel):
    ev_range_km: float | None = None
    total_range_km: float | None = None
    odometer: float | None = None
    climate_on: bool | None = None
    fuel_pct: float | None = None
    battery_12v_pct: float | None = None


class VehicleStatus(BaseModel):
    vehicle: str
    core: VehicleCore
    extras: VehicleExtras | None = None


class SleepCore(BaseModel):
    sleep_score: int | None = None
    total_sleep: str | None = None
    hrv: float | None = None
    resting_hr: float | None = None
    deep_sleep: str | None = None
    rem_sleep: str | None = None
    light_sleep: str | None = None


class SleepExtras(BaseModel):
    body_battery: int | None = None
    stress_avg: float | None = None
    hrv_weekly: float | None = None
    hrv_status: str | None = None


class SleepSummary(BaseModel):
    person: str
    source: str = Field(default="garmin")
    core: SleepCore
    extras: SleepExtras | None = None
