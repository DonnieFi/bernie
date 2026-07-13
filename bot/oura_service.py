import asyncio
import logging
import os
from datetime import date, timedelta

import aiohttp

from http_session import get_http_session

log = logging.getLogger("bernie.oura")

_BASE = "https://api.ouraring.com/v2/usercollection"
_LOOKBACK_DAYS = 14


def _token() -> str | None:
    return os.environ.get("OURA_TOKEN")


async def _fetch(path: str, params: dict) -> dict | None:
    token = _token()
    if not token:
        log.error("Oura: OURA_TOKEN not set")
        return None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        session = get_http_session()
        async with session.get(
            f"{_BASE}/{path}",
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 401:
                log.error("Oura: invalid or expired token")
                return None
            if resp.status != 200:
                log.error(f"Oura {path}: HTTP {resp.status}")
                return None
            return await resp.json()
    except Exception as e:
        log.error(f"Oura {path} failed: {e}")
        return None


async def get_sleep(target_date: date | None = None) -> dict | None:
    """Fetch sleep for ``target_date`` (default yesterday).

    family-bot-1bf.5: one ``sleep`` window over lookback days (not 14 sequential
    day fetches), then gather ``daily_sleep`` + ``daily_readiness`` for the
    resolved day.
    """
    if target_date is None:
        target_date = date.today() - timedelta(days=1)

    # Oura filters by bedtime window; end_date is exclusive. Client-filter by `day`.
    window_start = target_date - timedelta(days=_LOOKBACK_DAYS - 1)
    window_end = target_date + timedelta(days=1)
    sleep_data = await _fetch(
        "sleep",
        {
            "start_date": window_start.isoformat(),
            "end_date": window_end.isoformat(),
        },
    )
    if sleep_data is None:
        return None

    # Prefer exact target day; else most recent day in the lookback window.
    by_day: dict[str, list] = {}
    for s in sleep_data.get("data", []) or []:
        day = s.get("day")
        if not day:
            continue
        try:
            d = date.fromisoformat(day)
        except ValueError:
            continue
        if window_start <= d <= target_date:
            by_day.setdefault(day, []).append(s)

    if not by_day:
        return {"date": target_date.isoformat(), "no_data": True}

    found_date = max(by_day.keys())
    sessions = by_day[found_date]
    if found_date != target_date.isoformat():
        log.info(
            "Oura: no data for %s, using most recent available (%s)",
            target_date,
            found_date,
        )

    date_str = found_date
    end_str = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()
    params = {"start_date": date_str, "end_date": end_str}
    daily_data, readiness_data = await asyncio.gather(
        _fetch("daily_sleep", params),
        _fetch("daily_readiness", params),
    )

    s = max(sessions, key=lambda x: x.get("total_sleep_duration", 0) or 0)

    def mins(seconds):
        return round(seconds / 60) if seconds is not None else None

    result = {
        "date": date_str,
        "session_type": s.get("type"),
        "bedtime_start": s.get("bedtime_start"),
        "bedtime_end": s.get("bedtime_end"),
        "total_sleep_minutes": mins(s.get("total_sleep_duration")),
        "time_in_bed_minutes": mins(s.get("time_in_bed")),
        "awake_minutes": mins(s.get("awake_time")),
        "rem_minutes": mins(s.get("rem_sleep_duration")),
        "light_minutes": mins(s.get("light_sleep_duration")),
        "deep_minutes": mins(s.get("deep_sleep_duration")),
        "sleep_latency_minutes": mins(s.get("sleep_latency")),
        "sleep_efficiency": s.get("efficiency"),
        "restless_periods": s.get("restless_periods"),
        "average_hrv": s.get("average_hrv"),
        "lowest_heart_rate": s.get("lowest_heart_rate"),
        "average_heart_rate": s.get("average_heart_rate"),
        "average_breath": s.get("average_breath"),
        "average_spo2": s.get("average_spo2_percentage"),
        "lowest_spo2": s.get("lowest_spo2_percentage"),
        "skin_temp_deviation": s.get("average_skin_temperature"),
        "hrv_5min_samples": s.get("hrv", {}).get("items") if s.get("hrv") else None,
        "heart_rate_5min_samples": s.get("heart_rate", {}).get("items") if s.get("heart_rate") else None,
        "sleep_phase_5min": s.get("sleep_phase_5_min"),
        "movement_30sec": s.get("movement_30_sec"),
    }

    if daily_data:
        daily_sessions = daily_data.get("data", [])
        if daily_sessions:
            d = daily_sessions[0]
            result["daily_score"] = d.get("score")
            contrib = d.get("contributors", {})
            result["score_contributors"] = {
                "deep_sleep": contrib.get("deep_sleep"),
                "efficiency": contrib.get("efficiency"),
                "latency": contrib.get("latency"),
                "rem_sleep": contrib.get("rem_sleep"),
                "restfulness": contrib.get("restfulness"),
                "timing": contrib.get("timing"),
                "total_sleep": contrib.get("total_sleep"),
            }

    if readiness_data:
        readiness_sessions = readiness_data.get("data", [])
        if readiness_sessions:
            r = readiness_sessions[0]
            result["readiness_score"] = r.get("score")
            rc = r.get("contributors", {})
            result["readiness_contributors"] = {
                "activity_balance": rc.get("activity_balance"),
                "body_temperature": rc.get("body_temperature"),
                "hrv_balance": rc.get("hrv_balance"),
                "previous_day_activity": rc.get("previous_day_activity"),
                "previous_night": rc.get("previous_night"),
                "recovery_index": rc.get("recovery_index"),
                "resting_heart_rate": rc.get("resting_heart_rate"),
                "sleep_balance": rc.get("sleep_balance"),
            }

    return result
