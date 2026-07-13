"""Ports-and-adapters layer for presence WiFi/client discovery."""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import aiohttp

from http_session import get_http_session

log = logging.getLogger(__name__)


@runtime_checkable
class PresenceAdapter(Protocol):
    """Fetch active network clients as mac → {essid, ip?}."""

    async def fetch_active_clients(self) -> dict[str, dict[str, Any]]: ...


class UniFiPresenceAdapter:
    def __init__(self, host: str, api_key: str | None):
        self.host = host.rstrip("/")
        self.api_key = api_key

    async def fetch_active_clients(self) -> dict[str, dict[str, Any]]:
        if not self.api_key:
            return {}
        url = f"{self.host}/proxy/network/api/s/default/stat/sta"
        headers = {"x-api-key": self.api_key}
        try:
            session = get_http_session()
            async with session.get(
                    url,
                    headers=headers,
                    ssl=False,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        log.error("Unifi API error: %s", resp.status)
                        return {}
                    data = await resp.json()
                    clients = data.get("data", [])
                    return {
                        c["mac"].lower(): {"essid": c.get("essid", "Unifi"), "ip": c.get("ip")}
                        for c in clients
                        if c.get("mac")
                    }
        except Exception as e:
            log.error("Failed to fetch Unifi clients: %s", e)
            return {}


class HANetworkPresenceAdapter:
    def __init__(self, cfg: dict | None = None):
        self._cfg = cfg or {}

    async def fetch_active_clients(self) -> dict[str, dict[str, Any]]:
        from config import config as runtime_config
        from ha_service import ha_service

        cfg = self._cfg or runtime_config
        scanner_entity = cfg.get("home_assistant", {}).get("network_scanner_entity")
        if not scanner_entity:
            return {}
        try:
            state = await ha_service.get_state(scanner_entity)
            if not state:
                log.warning(
                    "Network scanner entity not found: %s — update home_assistant.network_scanner_entity",
                    scanner_entity,
                )
                return {}
            devices = state.get("attributes", {}).get("devices", [])
            essid = state.get("attributes", {}).get("friendly_name", "Google WiFi")
            return {
                d.get("mac").lower(): {"essid": essid, "ip": None}
                for d in devices
                if isinstance(d, dict) and d.get("mac")
            }
        except Exception as e:
            log.warning("Failed to fetch HA network MACs from %s: %s", scanner_entity, e)
            return {}


# Alias for roadmap plan alignment
HAPresenceAdapter = HANetworkPresenceAdapter

