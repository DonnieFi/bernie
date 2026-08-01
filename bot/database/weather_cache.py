"""database.weather_cache — location + snapshot helpers (family-bot-8lx.6)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone

from database.conn import _db_conn, _db_read

log = logging.getLogger("database.weather_cache")

_LOCATION_CACHE_TTL_DAYS = 30

_WEATHER_SNAPSHOT_TTL_SECONDS: dict[str, int] = {
    "current": 1800,
    "week": 10800,
}


async def get_weather_location(query_normalized: str) -> dict | None:
    async with _db_read() as db:
        cur = await db.execute(
            """SELECT query_normalized, display_name, lat, lon, country_code, country, admin1, timezone, source, created_at
               FROM weather_location_cache
               WHERE query_normalized=?""",
            (query_normalized,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        try:
            dt = datetime.fromisoformat(row[9])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=dt_timezone.utc)
            if (datetime.now(dt_timezone.utc) - dt).days >= _LOCATION_CACHE_TTL_DAYS:
                return None
        except Exception:
            pass
        return {
            "query_normalized": row[0],
            "display_name": row[1],
            "lat": row[2],
            "lon": row[3],
            "country_code": row[4],
            "country": row[5],
            "admin1": row[6],
            "timezone": row[7],
            "source": row[8],
            "created_at": row[9],
        }


async def save_weather_location(
    query_normalized: str,
    display_name: str,
    lat: float,
    lon: float,
    country_code: str | None = None,
    country: str | None = None,
    admin1: str | None = None,
    timezone: str | None = None,
    source: str | None = None,
):
    async with _db_conn() as db:
        await db.execute(
            """INSERT INTO weather_location_cache
               (query_normalized, display_name, lat, lon, country_code, country, admin1, timezone, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(query_normalized) DO UPDATE SET
                   display_name=excluded.display_name,
                   lat=excluded.lat,
                   lon=excluded.lon,
                   country_code=excluded.country_code,
                   country=excluded.country,
                   admin1=excluded.admin1,
                   timezone=excluded.timezone,
                   source=excluded.source,
                   created_at=excluded.created_at""",
            (
                query_normalized,
                display_name,
                lat,
                lon,
                country_code,
                country,
                admin1,
                timezone,
                source,
                datetime.now(dt_timezone.utc).isoformat(),
            ),
        )
        await db.commit()


async def get_weather_snapshot(lat: float, lon: float, kind: str = "current") -> dict | None:
    import json
    key = f"{kind}:{lat:.4f},{lon:.4f}"
    ttl = _WEATHER_SNAPSHOT_TTL_SECONDS.get(kind, 1800)
    async with _db_read() as db:
        cur = await db.execute(
            "SELECT data, fetched_at FROM weather_cache WHERE source=? ORDER BY id DESC LIMIT 1",
            (key,)
        )
        row = await cur.fetchone()
    if not row:
        return None
    try:
        dt = datetime.fromisoformat(row[1])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        if (datetime.now(dt_timezone.utc) - dt).total_seconds() > ttl:
            return None
    except Exception:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


async def set_weather_snapshot(lat: float, lon: float, data, kind: str = "current") -> None:
    import json
    key = f"{kind}:{lat:.4f},{lon:.4f}"
    now = datetime.now(dt_timezone.utc).isoformat()
    try:
        serialized = json.dumps(data, default=str)
    except Exception as e:
        log.warning(f"Failed to serialize weather snapshot: {e}")
        return
    async with _db_conn() as db:
        await db.execute("DELETE FROM weather_cache WHERE source=?", (key,))
        await db.execute(
            "INSERT INTO weather_cache (source, data, fetched_at) VALUES (?, ?, ?)",
            (key, serialized, now)
        )
        await db.commit()
