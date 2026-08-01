import logging
import time

import aiohttp

from config import config

log = logging.getLogger(__name__)


class FrigateService:
    def __init__(self):
        cfg = config.get("frigate", {})
        self.base_url = cfg.get("host", "http://frigate.lan:5000")
        self.cameras: dict[str, str] = cfg.get("cameras", {
            "cam_8": "Kitchen (cam 8)",
            "cam_18": "Front Door (cam 18)",
        })
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, bytes, str]] = {}
        self._CACHE_TTL = 30  # 30 seconds

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # family-bot-1bf.7: never bare ClientSession without timeout
            from http_session import DEFAULT_CLIENT_TIMEOUT

            self._session = aiohttp.ClientSession(timeout=DEFAULT_CLIENT_TIMEOUT)
        return self._session

    async def get_snapshot(self, camera: str, use_cache: bool = True) -> tuple[bytes, str] | None:
        if camera not in self.cameras:
            log.warning("Frigate: rejected unknown camera %r", camera)
            return None
        if use_cache and camera in self._cache:
            ts, data, content_type = self._cache[camera]
            if (time.monotonic() - ts) < self._CACHE_TTL:
                log.debug(f"Serving cached snapshot for {camera}")
                return data, content_type

        url = f"{self.base_url}/api/{camera}/latest.jpg"
        try:
            session = await self._get_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    content_type = resp.headers.get("Content-Type", "image/jpeg")
                    self._cache[camera] = (time.monotonic(), data, content_type)
                    return data, content_type
                log.error(f"Frigate snapshot error: HTTP {resp.status} for {camera}")
                return None
        except Exception as e:
            log.error(f"Frigate get_snapshot failed for {camera}: {e}")
            return None


    async def get_event_snapshot(self, event_id: str, crop: bool = True) -> tuple[bytes, str] | None:
        if crop:
            url = f"{self.base_url}/api/events/{event_id}/snapshot.jpg?crop=1&bbox=1"
        else:
            url = f"{self.base_url}/api/events/{event_id}/snapshot.jpg?crop=0"
        try:
            session = await self._get_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    content_type = resp.headers.get("Content-Type", "image/jpeg")
                    return data, content_type
                log.error(f"Frigate event snapshot error: HTTP {resp.status} for event {event_id}")
                return None
        except Exception as e:
            log.error(f"Frigate get_event_snapshot failed for {event_id}: {e}")
            return None


    async def get_events(self, limit: int = 20, camera: str | None = None) -> list[dict] | None:
        url = f"{self.base_url}/api/events"
        params = {"limit": str(limit)}
        if camera:
            params["cameras"] = camera
        try:
            session = await self._get_session()
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    return await resp.json()
                log.error(f"Frigate get_events error: HTTP {resp.status}")
                return None
        except Exception as e:
            log.error(f"Frigate get_events failed: {e}")
            return None


frigate_service = FrigateService()
