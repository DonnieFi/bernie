"""Weather service with city lookup, EC forecasts, and Tomorrow cross-checks."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Any

import aiohttp
from zoneinfo import ZoneInfo

from db_binding import get_database
from config import config
import db_writes

log = logging.getLogger(__name__)
_TIMEOUT = aiohttp.ClientTimeout(total=8)
_DEFAULT_TZ = ZoneInfo("America/Halifax")
_EC_BASE = "https://api.weather.gc.ca/collections"
_EC_COLLECTION_SWOB = "swob-realtime"
_EC_COLLECTION_CITY = "citypageweather-realtime"
_OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
_WEATHER_TTL = 1800  # 30 min per perf plan (locked) — match calendar
_weather_cache: dict[str, tuple[float, Any]] = {}
# 1bf.6 — single-flight: concurrent callers share one inflight Future per key
_weather_inflight: dict[str, asyncio.Future] = {}

_GEOCODE_STRIP_SUFFIXES = re.compile(
    r"\s+(city|town|village|municipality|county|district|region|province|metro|area)$",
    re.IGNORECASE,
)


def _weather_ttl() -> int:
    return int(config.get("context", {}).get("weather_cache_ttl_s", _WEATHER_TTL))


def _wcache_get(key: str) -> Any:
    entry = _weather_cache.get(key)
    ttl = _weather_ttl()
    if entry and (time.monotonic() - entry[0]) < ttl:
        return entry[1]
    return None


def _wcache_set(key: str, value: Any) -> None:
    _weather_cache[key] = (time.monotonic(), value)


_TOMORROW_CONDITIONS: dict[int, str] = {
    1000: "Clear", 1100: "Mostly Clear", 1101: "Partly Cloudy",
    1102: "Mostly Cloudy", 1001: "Cloudy",
    2000: "Fog", 2100: "Light Fog",
    4000: "Drizzle", 4001: "Rain", 4200: "Light Rain", 4201: "Heavy Rain",
    5000: "Snow", 5001: "Flurries", 5100: "Light Snow", 5101: "Heavy Snow",
    6000: "Freezing Drizzle", 6001: "Freezing Rain",
    6200: "Light Freezing Rain", 6201: "Heavy Freezing Rain",
    7000: "Ice Pellets", 7101: "Heavy Ice Pellets", 7102: "Light Ice Pellets",
    8000: "Thunderstorm",
}


def _serialize_weather(data: dict) -> dict:
    result = dict(data)
    if isinstance(result.get("confidence"), ForecastConfidence):
        result["confidence"] = asdict(result["confidence"])
    return result


def _deserialize_weather(data: dict) -> dict:
    result = dict(data)
    conf = result.get("confidence")
    if isinstance(conf, dict) and "level" in conf:
        try:
            result["confidence"] = ForecastConfidence(**conf)
        except Exception:
            result["confidence"] = None
    return result


@dataclass
class ForecastConfidence:
    level: str
    explanation: str
    temp_variance: float
    precip_agreement: bool


def _default_location() -> dict:
    loc = config.get("location", {})
    return {
        "query_normalized": "halifax",
        "display_name": loc.get("label", "Halifax, NS"),
        "lat": loc.get("lat", 44.6476),
        "lon": loc.get("lon", -63.5728),
        "country_code": "CA",
        "country": "Canada",
        "admin1": "Nova Scotia",
        "timezone": config.get("timezone", "America/Halifax"),
        "source": "config",
    }


def _normalize_query(value: str) -> str:
    value = re.sub(r"[^a-z0-9\s]", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


def _city_label(result: dict) -> str:
    parts = [result.get("name")]
    if result.get("admin1"):
        parts.append(result["admin1"])
    if result.get("country"):
        parts.append(result["country"])
    return ", ".join([p for p in parts if p])


async def resolve_location(city: str | None, session: aiohttp.ClientSession) -> dict | None:
    if not city:
        return _default_location()

    normalized = _normalize_query(city)
    
    # Shortcut for family's primary location — bare "halifax" always means NS
    if normalized == "halifax" or (
        "halifax" in normalized and ("nova scotia" in normalized or " ns" in normalized)
    ):
        return _default_location()

    cached = await get_database().get_weather_location(normalized)
    if cached:
        return cached

    params = {"name": city, "count": 5, "format": "json", "language": "en"}
    try:
        async with session.get(_OPEN_METEO_GEOCODE, params=params, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                log.warning(f"Open-Meteo geocoding HTTP {resp.status} for query {city!r}")
                return None
            data = await resp.json()
    except Exception as e:
        log.warning(f"Open-Meteo geocoding failed for {city!r}: {e}")
        return None

    results = data.get("results") or []
    if not results:
        stripped = _GEOCODE_STRIP_SUFFIXES.sub("", normalized)
        if stripped != normalized:
            db_hit = await get_database().get_weather_location(stripped)
            if db_hit:
                log.info(f"Geocoding cache hit for stripped query {stripped!r} (from {city!r})")
                return db_hit
            log.info(f"Geocoding returned no results for {city!r}, retrying with {stripped!r}")
            params["name"] = stripped
            try:
                async with session.get(_OPEN_METEO_GEOCODE, params=params, timeout=_TIMEOUT) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results") or []
            except Exception as e:
                log.warning(f"Fallback geocoding failed for {stripped!r}: {e}")

        if not results:
            return None

    # Only ambiguous if both top results share the same city name — prevents false positives
    # like "New York" matching "York, Nebraska" via substring (different names, not ambiguous).
    same_name = (
        results[0].get("name", "").lower() == results[1].get("name", "").lower()
        if len(results) > 1 else False
    )
    ambiguous = same_name and (
        results[0].get("country_code") != results[1].get("country_code")
        or results[0].get("admin1") != results[1].get("admin1")
    )

    if ambiguous:
        # Prefer Canadian result silently — family is Canadian, "London" means London ON
        canadian = next((r for r in results[:3] if r.get("country_code") == "CA"), None)
        if canadian:
            log.info(f"Ambiguous query {city!r} — defaulting to Canadian result: {_city_label(canadian)}")
            results = [canadian] + [r for r in results if r is not canadian]
            ambiguous = False
        else:
            candidates = [_city_label(r) for r in results[:3]]
            log.info(f"Ambiguous city query {city!r}: {candidates}")
            return {"kind": "ambiguous", "options": candidates, "query": city}

    result = results[0]
    location = {
        "query_normalized": normalized,
        "display_name": _city_label(result),
        "lat": result.get("latitude"),
        "lon": result.get("longitude"),
        "country_code": result.get("country_code"),
        "country": result.get("country"),
        "admin1": result.get("admin1"),
        "timezone": result.get("timezone") or config.get("timezone", "America/Halifax"),
        "source": "open-meteo",
    }
    await db_writes.routed("save_weather_location", 
        normalized,
        location["display_name"],
        location["lat"],
        location["lon"],
        country_code=location["country_code"],
        country=location["country"],
        admin1=location["admin1"],
        timezone=location["timezone"],
        source=location["source"],
    )
    return location


async def _weather_from_tomorrow(lat: float, lon: float, session: aiohttp.ClientSession, global_model: bool = False) -> dict | None:
    data = await _fetch_tomorrow_current(lat, lon, session)
    if not data:
        return None
    confidence = (
        ForecastConfidence(level="Medium", explanation="Tomorrow.io only — no regional data", temp_variance=0.0, precip_agreement=True)
        if global_model
        else _single_source_confidence()
    )
    return {
        **data,
        "wind_dir": "",
        "wind_kmh": round(float(data.get("wind_kph") or 0)),
        "high": None,
        "low": None,
        "hourly": [],
        "confidence": confidence,
        "source": "tomorrow.io",
    }


async def get_weather(lat: float, lon: float, session: aiohttp.ClientSession, use_ec: bool = True) -> dict | None:
    cache_key = f"weather:{lat:.4f},{lon:.4f}:ec={int(bool(use_ec))}"
    hit = _wcache_get(cache_key)
    if hit is not None:
        return hit

    # 1bf.6 single-flight: join existing inflight fetch for this key
    existing = _weather_inflight.get(cache_key)
    if existing is not None and not existing.done():
        return await asyncio.shield(existing)

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _weather_inflight[cache_key] = fut
    try:
        result = await _fetch_weather_uncached(lat, lon, session, use_ec=use_ec)
        if not fut.done():
            fut.set_result(result)
        return result
    except Exception as e:
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        if _weather_inflight.get(cache_key) is fut:
            _weather_inflight.pop(cache_key, None)


async def _fetch_weather_uncached(
    lat: float, lon: float, session: aiohttp.ClientSession, *, use_ec: bool = True
) -> dict | None:
    cache_key = f"weather:{lat:.4f},{lon:.4f}:ec={int(bool(use_ec))}"
    # re-check memory + DB under single-flight ownership
    hit = _wcache_get(cache_key)
    if hit is not None:
        return hit
    db_hit = await get_database().get_weather_snapshot(lat, lon, kind="current")
    if db_hit is not None:
        restored = _deserialize_weather(db_hit)
        _wcache_set(cache_key, restored)
        return restored

    if not use_ec:
        result = await _weather_from_tomorrow(lat, lon, session, global_model=True)
    else:
        obs, fcst = await asyncio.gather(
            _fetch_ec_nearest_feature(_EC_COLLECTION_SWOB, lat, lon, session),
            _fetch_ec_nearest_feature(_EC_COLLECTION_CITY, lat, lon, session),
        )
        if obs is None and fcst is None:
            result = await _weather_from_tomorrow(lat, lon, session)
        else:
            primary = _parse_ec_current(obs, fcst)
            try:
                secondary = await _fetch_tomorrow_current(lat, lon, session)
                confidence = _assess_confidence(primary, secondary) if secondary else _single_source_confidence()
            except Exception as e:
                log.warning(f"Secondary weather fetch failed: {e}")
                secondary = None
                confidence = _single_source_confidence()
            if secondary and secondary.get("feels_like_c") is not None:
                primary["feels_like_c"] = secondary["feels_like_c"]
            primary["confidence"] = confidence
            result = primary

    if result is not None:
        _wcache_set(cache_key, result)
        await db_writes.routed("set_weather_snapshot", lat, lon, _serialize_weather(result), kind="current")
    return result


async def get_weather_week(lat: float, lon: float, session: aiohttp.ClientSession, tz_name: str | None = None, use_ec: bool = True) -> list[dict] | None:
    cache_key = f"week:{lat:.4f},{lon:.4f}:ec={int(bool(use_ec))}"
    hit = _wcache_get(cache_key)
    if hit is not None:
        return hit

    existing = _weather_inflight.get(cache_key)
    if existing is not None and not existing.done():
        return await asyncio.shield(existing)

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _weather_inflight[cache_key] = fut
    try:
        result = await _fetch_weather_week_uncached(lat, lon, session, tz_name=tz_name, use_ec=use_ec)
        if not fut.done():
            fut.set_result(result)
        return result
    except Exception as e:
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        if _weather_inflight.get(cache_key) is fut:
            _weather_inflight.pop(cache_key, None)


async def _fetch_weather_week_uncached(
    lat: float, lon: float, session: aiohttp.ClientSession, *, tz_name: str | None = None, use_ec: bool = True
) -> list[dict] | None:
    cache_key = f"week:{lat:.4f},{lon:.4f}:ec={int(bool(use_ec))}"
    hit = _wcache_get(cache_key)
    if hit is not None:
        return hit

    db_hit = await get_database().get_weather_snapshot(lat, lon, kind="week")
    if db_hit is not None:
        restored = [_deserialize_weather(d) for d in db_hit]
        _wcache_set(cache_key, restored)
        return restored

    if not use_ec:
        tomorrow_days = await _fetch_tomorrow_daily(lat, lon, session, tz_name=tz_name)
        result = tomorrow_days or None
    else:
        fcst = await _fetch_ec_nearest_feature(_EC_COLLECTION_CITY, lat, lon, session)
        if not fcst:
            result = await get_weather_week(lat, lon, session, tz_name=tz_name, use_ec=False)
        else:
            ec_days = _parse_ec_week(fcst, tz_name)
            tomorrow_days = await _fetch_tomorrow_daily(lat, lon, session, tz_name=tz_name)
            tomorrow_by_date = {item["date"]: item for item in tomorrow_days or []}
            for day in ec_days:
                match = tomorrow_by_date.get(day["date"])
                if match:
                    day["tomorrow"] = match
                    day["confidence"] = _assess_forecast_confidence(day, match)
            result = ec_days or None

    if result is not None:
        _wcache_set(cache_key, result)
        await db_writes.routed("set_weather_snapshot", lat, lon, [_serialize_weather(d) for d in result], kind="week")
    return result


async def get_weather_for_request(city: str | None, day: str, session: aiohttp.ClientSession, date_str: str | None = None) -> dict | None:
    location = await resolve_location(city, session)
    if not location:
        return {"kind": "error", "message": f"I couldn't find weather for {city or 'that location'}."}
    if location.get("kind") == "ambiguous":
        opts = ", ".join(f"**{o}**" for o in location["options"])
        return {"kind": "error", "message": f"'{location['query']}' matches multiple places — try being more specific. Options: {opts}"}

    kind = (day or "today").lower()
    # None country_code means geocoder returned no country — default to EC (try Canada first)
    use_ec = location.get("country_code") in (None, "CA") or location.get("country") == "Canada"
    if kind in ("current", "now"):
        w = await get_weather(location["lat"], location["lon"], session, use_ec=use_ec)
        if not w:
            return {"kind": "error", "message": "Weather data unavailable right now."}
        return {"kind": "current", "location": location, "weather": w}

    days = await get_weather_week(location["lat"], location["lon"], session, tz_name=location.get("timezone"), use_ec=use_ec)
    if not days:
        return {"kind": "error", "message": "Forecast data unavailable right now."}

    tz = _location_tz(location)
    today = datetime.now(tz).date()
    if kind == "today":
        target = today
    elif kind == "tomorrow":
        target = today + timedelta(days=1)
    elif kind == "specific":
        if not date_str:
            return {"kind": "error", "message": "Provide a date for a specific-day forecast."}
        try:
            target = date.fromisoformat(date_str)
        except ValueError:
            return {"kind": "error", "message": "Use YYYY-MM-DD for specific-day weather."}
    elif kind == "week":
        return {"kind": "week", "location": location, "days": days}
    else:
        target = today

    matched = next((d for d in days if d["date"] == target.isoformat()), None)
    if not matched:
        return {
            "kind": "error",
            "message": f"I only have forecast data for the next {len(days)} days, and {target.isoformat()} is out of range.",
        }

    current = await get_weather(location["lat"], location["lon"], session, use_ec=use_ec) if kind == "today" else None
    return {"kind": "day", "location": location, "day": matched, "current": current}


async def _fetch_ec_nearest_feature(collection: str, lat: float, lon: float, session: aiohttp.ClientSession) -> dict | None:
    radii = (0.5, 1.5, 5.0)
    now_utc = datetime.now(timezone.utc)
    for radius in radii:
        bbox = f"{lon - radius},{lat - radius},{lon + radius},{lat + radius}"
        url = f"{_EC_BASE}/{collection}/items?f=json&bbox={bbox}&limit=10"
        try:
            async with session.get(url, timeout=_TIMEOUT) as resp:
                if resp.status != 200:
                    log.warning(f"EC {collection} returned HTTP {resp.status} (bbox radius {radius})")
                    continue
                data = await resp.json()
        except Exception as e:
            log.warning(f"EC {collection} fetch failed: {e}")
            continue

        features = data.get("features") or []
        if not features:
            continue

        # Filter for recent features (within last 3 hours)
        recent = []
        for f in features:
            props = f.get("properties", {})
            ts_str = (
                props.get("date_tm-value") 
                or props.get("obs_date_tm") 
                or props.get("timestamp", {}).get("en")
                or props.get("currentConditions", {}).get("timestamp", {}).get("en")
            )
            if not ts_str:
                continue
            try:
                # Handle various timestamp formats from EC OGC API
                if ts_str.endswith("Z"):
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                else:
                    ts = datetime.fromisoformat(ts_str)
                
                if (now_utc - ts) < timedelta(hours=12):
                    recent.append(f)
            except Exception as e:
                log.debug(f"Failed to parse EC timestamp {ts_str!r}: {e}")
                continue

        if not recent:
            continue

        return min(recent, key=lambda f: _distance_km(lat, lon, f))
    return None


def _parse_ec_current(obs: dict | None, fcst: dict | None) -> dict:
    obs_props = (obs or {}).get("properties", {})
    fcst_props = (fcst or {}).get("properties", {})
    cc = fcst_props.get("currentConditions", {})

    condition = (cc.get("condition", {}).get("en") or "—")
    high = None
    low = None
    hourly = []

    # Try to get high/low from forecastGroup
    for fc in fcst_props.get("forecastGroup", {}).get("forecasts", [])[:4]:
        for t in fc.get("temperatures", {}).get("temperature", []):
            val = t.get("value", {}).get("en")
            cls = _cls(t)
            if val is None:
                continue
            if "high" in cls and high is None:
                high = round(val)
            elif "low" in cls and low is None:
                low = round(val)

    # Use hourlyForecastGroup for actual hourly data
    for hc in fcst_props.get("hourlyForecastGroup", {}).get("hourlyForecasts", [])[:24]:
        ts_str = hc.get("timestamp")
        if not ts_str:
            continue
        try:
            dt_utc = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            dt_local = dt_utc.astimezone(_DEFAULT_TZ)
            hour_val = dt_local.hour
            temp_val = hc.get("temperature", {}).get("value", {}).get("en")
            cond = hc.get("condition", {}).get("en", "—")
            prob = hc.get("lop", {}).get("value", {}).get("en", 0)
            if temp_val is not None:
                hourly.append({"hour": hour_val, "temp_c": round(float(temp_val)), "precip_prob_pct": round(float(prob)), "condition": cond})
        except Exception:
            pass

    temp = round(float(obs_props.get("air_temp"))) if obs_props.get("air_temp") is not None else None
    if temp is None:
        cc_temp = cc.get("temperature", {}).get("value", {}).get("en")
        if cc_temp is not None:
            temp = round(float(cc_temp))

    return {
        "temp_c": temp,
        "condition": condition,
        "wind_kmh": round(float(obs_props.get("avg_wnd_spd_10m_pst1mt") or 0)),
        "wind_dir": _wind_dir(obs_props.get("avg_wnd_dir_10m_pst1mt") or 0),
        "dewpoint_c": round(float(obs_props.get("dwpt_temp")), 1) if obs_props.get("dwpt_temp") is not None else None,
        "high": high,
        "low": low,
        "precip_prob_pct": _precip_pct(condition),
        "hourly": hourly,
        "source": "environment-canada",
    }


def _parse_ec_week(fcst: dict, tz_name: str | None = None) -> list[dict]:
    props = (fcst or {}).get("properties", {})
    forecasts = props.get("forecastGroup", {}).get("forecasts", [])
    tz = _location_tz({"timezone": tz_name}) if tz_name else _DEFAULT_TZ
    today = datetime.now(tz).date()
    result: list[dict] = []
    i = 0

    if forecasts:
        first = forecasts[0].get("period", {}).get("textForecastName", {}).get("en", "")
        if "night" in first.lower() or first.lower() == "tonight":
            i = 1

    while i < len(forecasts) and len(result) < 7:
        fc = forecasts[i]
        name = fc.get("period", {}).get("textForecastName", {}).get("en", "")
        summary = fc.get("textSummary", {}).get("en", "") or ""

        if "night" in name.lower():
            i += 1
            continue

        high = None
        for t in fc.get("temperatures", {}).get("temperature", []):
            if "high" in _cls(t).lower():
                v = t.get("value", {}).get("en")
                if v is not None:
                    high = round(v)

        low = None
        if i + 1 < len(forecasts):
            nfc = forecasts[i + 1]
            nname = nfc.get("period", {}).get("textForecastName", {}).get("en", "")
            if "night" in nname.lower():
                for t in nfc.get("temperatures", {}).get("temperature", []):
                    if "low" in _cls(t).lower():
                        v = t.get("value", {}).get("en")
                        if v is not None:
                            low = round(v)

        result.append(
            {
                "date": _period_date(name, today).isoformat(),
                "label": name,
                "condition": summary.split(".")[0][:60] or "—",
                "summary": summary,
                "high": high,
                "low": low,
                "precip_pct": _precip_pct(summary),
                "source": "environment-canada",
            }
        )
        i += 2

    return result


async def _fetch_tomorrow_current(lat: float, lon: float, session: aiohttp.ClientSession) -> dict | None:
    api_key = os.getenv("TOMORROW_WEATHER_API")
    if not api_key:
        return None

    url = f"https://api.tomorrow.io/v4/weather/realtime?location={lat},{lon}&apikey={api_key}&units=metric"
    try:
        async with session.get(url, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as e:
        log.warning(f"Tomorrow current fetch failed: {e}")
        return None

    values = (data.get("data") or {}).get("values") or {}
    code = values.get("weatherCode")
    return {
        "temp_c": values.get("temperature"),
        "feels_like_c": values.get("temperatureApparent"),
        "precip_prob_pct": values.get("precipitationProbability", 0),
        "wind_kph": (values.get("windSpeed") or 0) * 3.6,
        "condition": _TOMORROW_CONDITIONS.get(code, "—") if code is not None else "—",
        "source": "tomorrow.io",
    }


async def _fetch_tomorrow_daily(lat: float, lon: float, session: aiohttp.ClientSession, tz_name: str | None = None) -> list[dict] | None:
    api_key = os.getenv("TOMORROW_WEATHER_API")
    if not api_key:
        return None

    url = f"https://api.tomorrow.io/v4/weather/forecast?location={lat},{lon}&timesteps=1d&apikey={api_key}&units=metric"
    try:
        async with session.get(url, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as e:
        log.warning(f"Tomorrow forecast fetch failed: {e}")
        return None

    payload = data.get("data") or data
    timelines = payload.get("timelines") or data.get("timelines") or {}
    
    intervals = []
    if isinstance(timelines, dict) and "daily" in timelines:
        intervals = timelines["daily"]
    elif isinstance(timelines, list):
        timeline = next((t for t in timelines if isinstance(t, dict) and t.get("timestep") == "1d"), None)
        if not timeline and timelines and isinstance(timelines[0], dict):
            timeline = timelines[0]
        intervals = timeline.get("intervals", []) if timeline else []

    if not intervals:
        return None

    result: list[dict] = []
    tz = _location_tz({"timezone": tz_name}) if tz_name else _DEFAULT_TZ
    for interval in intervals[:7]:
        values = interval.get("values", {})
        start = interval.get("time") or interval.get("startTime", "")
        local_date = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(tz).date() if start else None
        
        # Daily intervals often use temperatureAvg instead of temperature
        temp_c = values.get("temperature")
        if temp_c is None:
            temp_c = values.get("temperatureAvg")

        result.append(
            {
                "date": local_date.isoformat() if local_date else start[:10],
                "label": local_date.strftime("%A") if local_date else "",
                "temp_c": temp_c,
                "temp_max_c": values.get("temperatureMax"),
                "temp_min_c": values.get("temperatureMin"),
                "feels_like_c": values.get("temperatureApparent"),
                "precip_pct": values.get("precipitationProbability", 0),
                "wind_kph": (values.get("windSpeed") or 0) * 3.6 if values.get("windSpeed") is not None else None,
                "source": "tomorrow.io",
            }
        )
    return result or None


def _assess_confidence(primary: dict, secondary: dict | None) -> ForecastConfidence:
    if not secondary:
        return _single_source_confidence()
    temp_variance = abs((primary.get("temp_c") or 0) - (secondary.get("temp_c") or 0))
    precip_diff = abs(primary.get("precip_prob_pct", 0) - secondary.get("precip_prob_pct", 0))
    precip_agreement = precip_diff < 25

    if temp_variance > 4 or not precip_agreement:
        return ForecastConfidence(
            level="Low",
            explanation=f"Models disagree — {temp_variance:.1f}°C temp variance" + ("" if precip_agreement else ", rain timing uncertain"),
            temp_variance=temp_variance,
            precip_agreement=precip_agreement,
        )
    if temp_variance > 2 or precip_diff > 15:
        return ForecastConfidence(
            level="Medium",
            explanation="Models mostly agree with minor differences",
            temp_variance=temp_variance,
            precip_agreement=precip_agreement,
        )
    return ForecastConfidence(level="High", explanation="Models agree", temp_variance=temp_variance, precip_agreement=precip_agreement)


def _assess_forecast_confidence(ec_day: dict, tomorrow_day: dict | None) -> ForecastConfidence:
    if not tomorrow_day:
        return _single_source_confidence()
    tomorrow_high = tomorrow_day.get("temp_max_c") or tomorrow_day.get("temp_c") or 0
    temp_variance = abs((ec_day.get("high") or 0) - tomorrow_high)
    precip_diff = abs(ec_day.get("precip_pct", 0) - tomorrow_day.get("precip_pct", 0))
    precip_agreement = precip_diff < 25

    if temp_variance > 4 or not precip_agreement:
        return ForecastConfidence(
            level="Low",
            explanation=f"EC and Tomorrow disagree — {temp_variance:.1f}°C variance" + ("" if precip_agreement else ", rain timing uncertain"),
            temp_variance=temp_variance,
            precip_agreement=precip_agreement,
        )
    if temp_variance > 2 or precip_diff > 15:
        return ForecastConfidence(
            level="Medium",
            explanation="EC and Tomorrow mostly agree",
            temp_variance=temp_variance,
            precip_agreement=precip_agreement,
        )
    return ForecastConfidence(level="High", explanation="EC and Tomorrow agree", temp_variance=temp_variance, precip_agreement=precip_agreement)


def _single_source_confidence() -> ForecastConfidence:
    return ForecastConfidence(level="Medium", explanation="Single source — no cross-check available", temp_variance=0.0, precip_agreement=True)


def _wind_dir(degrees) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[round(float(degrees) / 22.5) % 16]


def _cls(t: dict) -> str:
    c = t.get("class", "")
    return c.get("en", "") if isinstance(c, dict) else c


def _precip_pct(text: str) -> int:
    m = re.search(r"(\d+)\s*percent\s*chance", text or "", re.IGNORECASE)
    return int(m.group(1)) if m else 0


def _period_date(period_name: str, today: date) -> date:
    day_name = period_name.split()[0].lower()
    if day_name in ("tonight", "today"):
        return today
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if day_name in days:
        target_weekday = days.index(day_name)
        current_weekday = today.weekday()
        delta = (target_weekday - current_weekday) % 7
        return today + timedelta(days=delta)
    log.warning(f"_period_date: unrecognized period name {period_name!r}, defaulting to today")
    return today


def _location_tz(location: dict):
    try:
        return ZoneInfo(location.get("timezone") or config.get("timezone", "America/Halifax"))
    except Exception:
        return _DEFAULT_TZ


def _distance_km(lat: float, lon: float, feature: dict) -> float:
    coords = (feature.get("geometry") or {}).get("coordinates") or []
    if len(coords) < 2:
        return float("inf")
    flon, flat = coords[0], coords[1]
    dlat = radians(flat - lat)
    dlon = radians(flon - lon)
    a = sin(dlat / 2) ** 2 + cos(radians(lat)) * cos(radians(flat)) * sin(dlon / 2) ** 2
    return 2 * 6371 * asin(sqrt(a))


def _confidence_tag(confidence) -> str:
    if not confidence:
        return ""
    level = confidence.level
    if level == "High":
        return ""
    return f" ⚠️ {level} Confidence"


def weather_line(w: dict) -> str:
    temp = f"{w['temp_c']}°C" if w.get("temp_c") is not None else "—°C"
    high = f"H {w['high']}°" if w.get("high") is not None else ""
    low = f"L {w['low']}°" if w.get("low") is not None else ""
    hl_parts = " / ".join(filter(None, [high, low]))
    hl = f"  ({hl_parts})" if hl_parts else ""
    wind = f"  💨 {w['wind_kmh']} km/h {w.get('wind_dir', '')}" if w.get("wind_kmh") is not None else ""
    conf = _confidence_tag(w.get("confidence"))
    return f"{w.get('condition', '—')} · {temp}{hl}{wind}{conf}"


def weather_forecast_line(day: dict) -> str:
    parts = [day.get("condition", "—")]
    if day.get("high") is not None or day.get("low") is not None:
        parts.append(f"H {day.get('high', '—')}° / L {day.get('low', '—')}°")
    elif day.get("temp_c") is not None:
        parts.append(f"{round(day['temp_c'])}°C")
        if day.get("feels_like_c") is not None:
            parts.append(f"Feels {round(day['feels_like_c'])}°")
    if day.get("precip_pct") is not None:
        parts.append(f"🌧 {day['precip_pct']}%")
    conf = _confidence_tag(day.get("confidence"))
    if conf:
        parts.append(conf.strip())
    return " · ".join(parts)


def format_weather_report(report: dict) -> str:
    if not report:
        return "Weather data unavailable right now."
    if report.get("kind") == "error":
        return report.get("message", "Weather data unavailable right now.")
    location = report.get("location", {}).get("display_name", "Halifax, NS")
    if report.get("kind") == "current":
        return f"{location}\n{weather_line(report['weather'])}"
    if report.get("kind") == "week":
        lines = [f"{location} — 7-day forecast"]
        for day in report.get("days", []):
            lines.append(f"- {day['label']}: {weather_forecast_line(day)}")
        return "\n".join(lines)
    if report.get("kind") == "day":
        day = report.get("day", {})
        lines = [f"{location} — {day.get('label', day.get('date', ''))}", weather_forecast_line(day)]
        if report.get("current"):
            lines.append(f"Now: {weather_line(report['current'])}")
        if day.get("tomorrow"):
            t = day["tomorrow"]
            lines.append(f"Tomorrow.io: {t.get('label', day.get('label', ''))} · temp {t.get('temp_c', '—')}°C · rain {t.get('precip_pct', 0)}%")
        return "\n".join(lines)
    return "Weather data unavailable right now."
