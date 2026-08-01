"""In-memory household snapshot refreshed by BTS (perf P2).

Hot-path reads avoid per-turn gather latency; staleness bounded by
``context.snapshot_refresh_min`` (default 5 minutes).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_snapshot: "LiveSnapshot | None" = None
_refresh_lock = asyncio.Lock()
_bts_consecutive_failures = 0


def record_bts_refresh_success() -> None:
    global _bts_consecutive_failures
    _bts_consecutive_failures = 0


def record_bts_refresh_failure() -> int:
    global _bts_consecutive_failures
    _bts_consecutive_failures += 1
    if _bts_consecutive_failures >= 3:
        log.warning(
            "live_snapshot BTS: %d consecutive refresh failures",
            _bts_consecutive_failures,
        )
    return _bts_consecutive_failures


@dataclass
class LiveSnapshot:
    presence: dict[str, Any] = field(default_factory=dict)
    weather: str = ""
    ha_states: list[dict] = field(default_factory=list)
    updated_monotonic: float = 0.0

    def age_s(self) -> float:
        if not self.updated_monotonic:
            return float("inf")
        return time.monotonic() - self.updated_monotonic

    def is_fresh(self, max_age_s: float) -> bool:
        return self.age_s() <= max_age_s


def get_live_snapshot() -> LiveSnapshot | None:
    return _snapshot


def set_live_snapshot(snap: LiveSnapshot | None) -> None:
    global _snapshot
    _snapshot = snap


def _snapshot_refresh_timeout_s(config: dict) -> float:
    ctx_cfg = config.get("context", {}) or {}
    raw = ctx_cfg.get("snapshot_refresh_timeout_s", 30)
    try:
        return max(5.0, float(raw))
    except (TypeError, ValueError):
        return 30.0


def _ha_friendly_name(state: dict) -> str:
    attrs = state.get("attributes") or {}
    return attrs.get("friendly_name", state.get("entity_id"))


async def refresh_live_snapshot(
    *,
    config: dict,
    cal_service=None,
    session=None,
) -> LiveSnapshot:
    """Refresh presence, weather, and slim HA into the process snapshot."""
    import asyncio as _aio

    snap = LiveSnapshot()
    t0 = time.monotonic()

    async def _presence():
        try:
            from presence_service import presence_service
            return await presence_service.get_presence()
        except Exception as e:
            log.warning("live_snapshot: presence error: %s", e)
            return {}

    async def _weather():
        if not session:
            return ""
        try:
            from weather_service import get_weather, weather_line
            lat = config.get("location", {}).get("lat", 44.6476)
            lon = config.get("location", {}).get("lon", -63.5728)
            w = await get_weather(lat, lon, session)
            return weather_line(w) if w else ""
        except Exception as e:
            log.warning("live_snapshot: weather error: %s", e)
            return ""

    async def _ha():
        try:
            from ha_service import ha_service
            ha_domains = (
                config.get("context", {}).get("ha_domains")
                or ["light", "switch", "media_player"]
            )
            live = await ha_service.get_live_states()
            return [
                {
                    "entity_id": s.get("entity_id"),
                    "state": s.get("state"),
                    "name": _ha_friendly_name(s),
                }
                for s in live
                if s.get("entity_id", "").split(".")[0] in ha_domains
            ]
        except Exception as e:
            log.warning("live_snapshot: HA error: %s", e)
            return []

    presence, weather, ha_states = await _aio.gather(
        _presence(), _weather(), _ha(), return_exceptions=True,
    )
    if isinstance(presence, BaseException):
        presence = {}
    if isinstance(weather, BaseException):
        weather = ""
    if isinstance(ha_states, BaseException):
        ha_states = []

    snap.presence = presence or {}
    snap.weather = weather or ""
    snap.ha_states = ha_states or []
    snap.updated_monotonic = time.monotonic()
    set_live_snapshot(snap)
    log.info(
        "live_snapshot refreshed in %dms (presence=%d ha=%d)",
        int((time.monotonic() - t0) * 1000),
        len(snap.presence),
        len(snap.ha_states),
    )
    return snap


async def ensure_fresh_snapshot(
    *,
    config: dict,
    cal_service=None,
    session=None,
) -> LiveSnapshot | None:
    """Return a snapshot without blocking the chat hot path (family-bot-2wh.12).

    If fresh → return it. If stale → return last snapshot immediately and kick
    an async refresh under lock. First-ever snapshot still waits (with timeout).
    """
    ctx_cfg = config.get("context", {}) or {}
    if not ctx_cfg.get("snapshot_enabled", False):
        return None
    max_age = float(ctx_cfg.get("snapshot_refresh_min", 5)) * 60.0
    existing = get_live_snapshot()
    if existing and existing.is_fresh(max_age):
        return existing

    async def _refresh_bg() -> None:
        async with _refresh_lock:
            cur = get_live_snapshot()
            if cur and cur.is_fresh(max_age):
                return
            try:
                await asyncio.wait_for(
                    refresh_live_snapshot(
                        config=config, cal_service=cal_service, session=session,
                    ),
                    timeout=_snapshot_refresh_timeout_s(config),
                )
            except asyncio.TimeoutError:
                log.warning(
                    "live_snapshot bg refresh timed out after %.0fs",
                    _snapshot_refresh_timeout_s(config),
                )
            except Exception as e:
                log.warning("live_snapshot bg refresh failed: %s", e)

    if existing is not None:
        # Stale-but-present: never block chat; refresh in background
        asyncio.create_task(_refresh_bg())
        return existing

    # No snapshot yet — wait once (startup) with timeout
    async with _refresh_lock:
        existing = get_live_snapshot()
        if existing and existing.is_fresh(max_age):
            return existing
        try:
            return await asyncio.wait_for(
                refresh_live_snapshot(
                    config=config, cal_service=cal_service, session=session,
                ),
                timeout=_snapshot_refresh_timeout_s(config),
            )
        except asyncio.TimeoutError:
            log.warning(
                "live_snapshot ensure_fresh timed out after %.0fs",
                _snapshot_refresh_timeout_s(config),
            )
            return get_live_snapshot()
        except Exception as e:
            log.warning("live_snapshot ensure_fresh failed: %s", e)
            return get_live_snapshot()
