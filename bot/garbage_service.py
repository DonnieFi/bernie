"""Halifax garbage/recycling schedule via ReCollect ICS."""
import asyncio
import logging
import time
from datetime import datetime, date, timedelta

import aiohttp
from datetime import tzinfo
from zoneinfo import ZoneInfo

_ICS_TTL = 604_800  # 7 days — schedule is published weeks in advance
_ics_cache: dict[str, tuple[float, str]] = {}
_ics_locks: dict[str, asyncio.Lock] = {}

log = logging.getLogger(__name__)

ICONS = {
    "garbage":   "🗑️",
    "organics":  "♻️",
    "recycling": "♻️",
}


def _icon(summary: str) -> str:
    s = summary.lower()
    for key, icon in ICONS.items():
        if key in s:
            return icon
    return "🚛"


def _is_curbside(summary: str) -> bool:
    skip = ["depot", "clothing swap", "mobile hsw", "hsw event"]
    s = summary.lower()
    return not any(w in s for w in skip)


def _parse_ics(text: str) -> list[dict]:
    text = text.replace("\r\n ", "").replace("\r\n\t", "").replace("\n ", "").replace("\n\t", "")
    events = []
    current: dict = {}
    for line in text.splitlines():
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if "date" in current and "summary" in current:
                events.append(current)
            else:
                if current:
                    log.debug(f"Skipped malformed ICS event: {current}")
            current = {}
        elif line.startswith("DTSTART"):
            date_str = line.split(":")[-1].strip()
            try:
                current["date"] = datetime.strptime(date_str, "%Y%m%d").date()
            except ValueError:
                pass
        elif line.startswith("SUMMARY:"):
            current["summary"] = line[8:].replace("\\,", ",")
    return sorted(events, key=lambda e: e["date"])


def _clean_summary(summary: str) -> str:
    s = summary.lower()
    parts = []
    if "garbage" in s:
        parts.append("Garbage")
    if "organic" in s or "green" in s:
        parts.append("Green Bin")
    if "recycl" in s:
        parts.append("Recycling")
    
    if not parts:
        return "Collection"
        
    if len(parts) == 1:
        return parts[0]
    elif len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    else:
        return f"{', '.join(parts[:-1])}, and {parts[-1]}"


async def _fetch_ics(url: str, session: aiohttp.ClientSession) -> str | None:
    now = time.monotonic()
    entry = _ics_cache.get(url)
    if entry and now - entry[0] < _ICS_TTL:
        return entry[1]
    lock = _ics_locks.setdefault(url, asyncio.Lock())
    async with lock:
        # Re-check after acquiring lock — another coroutine may have fetched while we waited
        entry = _ics_cache.get(url)
        if entry and time.monotonic() - entry[0] < _ICS_TTL:
            return entry[1]
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    log.error(f"ReCollect returned HTTP {resp.status} — serving stale cache" if entry else f"ReCollect returned HTTP {resp.status} — no cached data")
                    return entry[1] if entry else None
                text = await resp.text()
            _ics_cache[url] = (time.monotonic(), text)
            log.debug(f"ReCollect ICS fetched and cached ({len(text)} bytes)")
            return text
        except Exception as e:
            log.error(f"ReCollect fetch failed: {e}" + (" — serving stale cache" if entry else ""))
            return entry[1] if entry else None


async def get_next_collections(ics_url: str, tz: tzinfo, session: aiohttp.ClientSession, days: int = 7) -> list[dict]:
    text = await _fetch_ics(ics_url, session)
    if text is None:
        return []

    today = datetime.now(tz).date()
    cutoff = today + timedelta(days=days)
    return [
        {"date": e["date"], "summary": _clean_summary(e["summary"]), "icon": _icon(e["summary"])}
        for e in _parse_ics(text)
        if today <= e["date"] <= cutoff and _is_curbside(e["summary"])
    ]


async def get_tomorrow_collection(ics_url: str, tz: tzinfo, session: aiohttp.ClientSession) -> dict | None:
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    events = await get_next_collections(ics_url, tz, session, days=2)
    for e in events:
        if e["date"] == tomorrow:
            return e
    return None
