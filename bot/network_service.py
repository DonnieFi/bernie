import asyncio
import json
import logging
import os
import time
import pathlib
import aiohttp
from typing import Dict, Any, List, Optional
from config import config
from ha_service import ha_service

log = logging.getLogger(__name__)

def normalize_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


class NetworkService:
    def __init__(self):
        self.store_path = pathlib.Path("/data/network_devices.json")
        self.lock = asyncio.Lock()
        self.valid_statuses = {"confirmed", "suspected", None}
        self._session: aiohttp.ClientSession | None = None
        self.vendor_map = {
            "raspberry pi": "server",
            "sonos": "speaker",
            "slim devices": "speaker",
            "harman": "speaker",
            "nintendo": "console",
            "espressif": "iot",
            "philips": "hub",
            "apple": "phone",
            "google": "router",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # family-bot-1bf.7: never bare ClientSession without timeout
            from http_session import DEFAULT_CLIENT_TIMEOUT

            self._session = aiohttp.ClientSession(timeout=DEFAULT_CLIENT_TIMEOUT)
        return self._session

    async def _load_stored(self) -> Dict[str, Any]:
        if not self.store_path.exists():
            return {}
        try:
            raw = await asyncio.to_thread(self.store_path.read_text)
            return json.loads(raw)
        except Exception as e:
            log.warning(f"network_devices.json unreadable: {e}")
            return {}

    async def _save_stored(self, data: Dict[str, Any]) -> bool:
        try:
            from db_client import writes_locally
            import db_writes

            if writes_locally():
                await asyncio.to_thread(
                    self.store_path.write_text, json.dumps(data, indent=2)
                )
            else:
                await db_writes.routed("save_network_devices_store", data=data)
            return True
        except Exception as e:
            log.error(f"Failed to save network devices: {e}")
            return False

    def _get_vendor_kind(self, vendor: Optional[str]) -> str:
        v = str(vendor or "").lower()
        for key, kind in self.vendor_map.items():
            if key in v:
                return kind
        return "unknown"

    def _format_seen(self, ts: Optional[float]) -> Optional[str]:
        if ts is None:
            return None
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            return None
        if ts > 1e12:
            ts /= 1000
        diff = time.time() - ts
        if diff < 0 or diff > 86400 * 3650:
            return None
        if diff < 90:     return "now"
        if diff < 3600:   return f"{int(diff/60)}m ago"
        if diff < 86400:  return f"{int(diff/3600)}h ago"
        return f"{int(diff/86400)}d ago"

    async def _unifi_get_json(
        self,
        path: str,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout_s: float = 5.0,
    ) -> list | None:
        """GET UniFi controller path; return ``data`` list or None."""
        presence_cfg = config.get("presence", {})
        unifi_host = presence_cfg.get("unifi_host", "https://192.168.1.X")
        ssl_verify = bool(presence_cfg.get("unifi_ssl_verify", False))
        unifi_key = os.environ.get("UNIFI_KEY")
        if not unifi_key:
            return None
        session = session or await self._get_session()
        url = f"{unifi_host}{path}"
        try:
            async with session.get(
                url,
                headers={"x-api-key": unifi_key},
                ssl=ssl_verify,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                if resp.status != 200:
                    log.warning("UniFi %s → HTTP %s", path, resp.status)
                    return None
                return (await resp.json()).get("data")
        except Exception as e:
            log.error("UniFi fetch failed %s: %s", path, e)
            return None

    async def fetch_unifi_snapshot(self) -> dict[str, Any]:
        """One UniFi pull for clients + infra (family-bot-1bf.3).

        Returns ``{alluser, sta, device}`` lists (or empty). Watchman +
        get_devices share this so poll does not hit ``stat/sta`` twice.
        """
        if not os.environ.get("UNIFI_KEY"):
            return {"alluser": [], "sta": [], "device": [], "available": False}
        try:
            session = await self._get_session()
            alluser, sta, device = await asyncio.gather(
                self._unifi_get_json(
                    "/proxy/network/api/s/default/stat/alluser",
                    session=session,
                ),
                self._unifi_get_json(
                    "/proxy/network/api/s/default/stat/sta",
                    session=session,
                ),
                self._unifi_get_json(
                    "/proxy/network/api/s/default/stat/device",
                    session=session,
                    timeout_s=10.0,
                ),
            )
            return {
                "alluser": alluser if alluser is not None else [],
                "sta": sta if sta is not None else [],
                "device": device if device is not None else [],
                "available": alluser is not None or sta is not None or device is not None,
                "sta_available": sta is not None,
            }
        except Exception as e:
            log.error("Unifi snapshot failed: %s", e)
            return {"alluser": [], "sta": [], "device": [], "available": False, "sta_available": False}

    @staticmethod
    def infra_from_unifi_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
        """AP offline list + client counts from a shared UniFi snapshot."""
        offline_aps: list[str] = []
        for dev in snap.get("device") or []:
            model = str(dev.get("model") or "")
            name = str(dev.get("name") or dev.get("mac") or "unknown")
            dev_type = str(dev.get("type") or "")
            is_ap = dev_type == "uap" or "UAP" in model or "U6" in model or "AC" in model
            if not is_ap:
                continue
            if dev.get("state") != 1:
                offline_aps.append(name)

        sta_raw = snap.get("sta")
        sta_available = bool(snap.get("sta_available", sta_raw is not None))
        wifi_count: int | None = None
        wired_count: int | None = None
        if sta_available and sta_raw is not None:
            wifi_count = sum(1 for c in sta_raw if not c.get("is_wired"))
            wired_count = sum(1 for c in sta_raw if c.get("is_wired"))

        return {
            "offline_aps": offline_aps,
            "wifi_clients": wifi_count,
            "wired_clients": wired_count,
            "sta_available": sta_available,
        }

    def _merge_unifi_into_devices(
        self,
        devices: Dict[str, Dict[str, Any]],
        snap: dict[str, Any],
    ) -> None:
        """Apply alluser + sta rows from a snapshot into ``devices`` (by MAC)."""
        for c in snap.get("alluser") or []:
            mac = normalize_mac(c.get("mac") or "")
            if mac:
                devices[mac] = {
                    "mac": mac,
                    "unifi_name": c.get("name") or "",
                    "hostname": c.get("hostname") or "",
                    "vendor": c.get("oui") or c.get("dev_vendor") or "",
                    "ip": c.get("ip") or c.get("last_ip") or "",
                    "is_wired": c.get("is_wired", False),
                    "network": "unifi",
                    "last_seen": c.get("last_seen"),
                    "essid": c.get("essid") or "",
                    "is_active": False,
                }
        for c in snap.get("sta") or []:
            mac = normalize_mac(c.get("mac") or "")
            if not mac:
                continue
            rx_bytes_r = (c.get("rx_bytes-r") or 0) + (c.get("wired-rx_bytes-r") or 0)
            tx_bytes_r = (c.get("tx_bytes-r") or 0) + (c.get("wired-tx_bytes-r") or 0)
            rx_mbps = round((rx_bytes_r * 8) / 1_000_000, 2)
            tx_mbps = round((tx_bytes_r * 8) / 1_000_000, 2)
            d = devices.get(mac, {})
            d.update({
                "mac": mac,
                "is_active": True,
                "last_seen": c.get("last_seen") or d.get("last_seen"),
                "ip": c.get("ip") or d.get("ip", ""),
                "unifi_name": c.get("name") or d.get("unifi_name", ""),
                "hostname": c.get("hostname") or d.get("hostname", ""),
                "essid": c.get("essid") or d.get("essid", ""),
                "network": "unifi" if d.get("network") != "both" else "both",
                "rx_mbps": rx_mbps,
                "tx_mbps": tx_mbps,
            })
            devices[mac] = d

    async def get_devices(
        self,
        *,
        unifi_snapshot: dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        stored = await self._load_stored()
        devices: Dict[str, Dict[str, Any]] = {}

        # 1. UniFi (shared snapshot when provided — family-bot-1bf.3)
        snap = unifi_snapshot
        if snap is None and os.environ.get("UNIFI_KEY"):
            snap = await self.fetch_unifi_snapshot()
        if snap:
            try:
                self._merge_unifi_into_devices(devices, snap)
            except Exception as e:
                log.error(f"Unifi merge failed: {e}")

        # 2. Fetch HA (Google WiFi) data
        scanner_entity = config.get("home_assistant", {}).get("network_scanner_entity", "")
        if scanner_entity:
            try:
                scanner = await ha_service.get_state(scanner_entity)
                if scanner:
                    for d in scanner.get("attributes", {}).get("devices", []):
                        mac = normalize_mac(d.get("mac") or "")
                        if not mac: continue
                        if mac in devices:
                            devices[mac]["network"] = "both"
                            devices[mac]["is_active"] = True
                            if not devices[mac]["hostname"]: devices[mac]["hostname"] = d.get("hostname", "")
                            if not devices[mac]["vendor"]: devices[mac]["vendor"] = str(d.get("vendor", ""))
                            if not devices[mac]["ip"]: devices[mac]["ip"] = d.get("ip", "")
                        else:
                            devices[mac] = {
                                "mac": mac,
                                "unifi_name": "",
                                "hostname": d.get("hostname") or "",
                                "vendor": str(d.get("vendor") or ""),
                                "ip": d.get("ip") or "",
                                "is_wired": False,
                                "network": "google",
                                "last_seen": None,
                                "is_active": True,
                                "essid": "",
                            }
            except Exception as e:
                log.error(f"HA scanner fetch failed: {e}")

        # 3. Add stored devices not found by scanners (Offline devices)
        for mac, entry in stored.items():
            if mac not in devices:
                devices[mac] = {
                    "mac": mac,
                    "unifi_name": "",
                    "hostname": "",
                    "vendor": "",
                    "ip": entry.get("ip", ""),
                    "is_wired": False,
                    "network": entry.get("network", "unknown"),
                    "last_seen": None,
                    "is_active": False,
                    "essid": "",
                }

        # 4. Process & Merge with Stored data
        result = []
        for mac, d in devices.items():
            entry = stored.get(mac, {})
            custom_name = entry.get("name", "")
            display_name = custom_name or d.get("unifi_name") or d.get("hostname") or ""
            
            # Prioritize stored IP if live IP is missing
            ip = d.get("ip", "") or entry.get("ip", "")

            seen_str = self._format_seen(d.get("last_seen"))
            if d.get("is_active"):
                seen_str = "now"
                d["last_seen"] = time.time()

            result.append({
                "mac": mac,
                "display_name": str(display_name),
                "custom_name": str(custom_name),
                "unifi_name": str(d.get("unifi_name", "")),
                "hostname": str(d.get("hostname", "")),
                "vendor": str(d.get("vendor", "")),
                "ip": str(ip),
                "is_wired": bool(d.get("is_wired", False)),
                "network": str(d.get("network", entry.get("network", "unknown"))),
                "last_seen": d.get("last_seen"),
                "is_active": bool(d.get("is_active", False)),
                "seen": seen_str or "offline",
                "essid": d.get("essid", ""),
                "kind": entry.get("kind") or self._get_vendor_kind(d.get("vendor")),
                "status": entry.get("status"),
                "unnamed": not bool(display_name),
                "rx_mbps": float(d.get("rx_mbps", 0.0)),
                "tx_mbps": float(d.get("tx_mbps", 0.0)),
            })

        # 5. Handle Aliases
        alias_to_primary = {}
        for m, entry in stored.items():
            for a in entry.get("aliases", []):
                alias_to_primary[a.lower()] = m.lower()

        mac_to_item = {r["mac"]: r for r in result}
        to_remove = set()
        for aliased_mac, primary_mac in alias_to_primary.items():
            if primary_mac not in mac_to_item:
                continue
            p = mac_to_item[primary_mac]
            if aliased_mac in mac_to_item:
                # Aliased device is currently visible — full live merge
                a = mac_to_item[aliased_mac]
                if p["network"] != a["network"]: p["network"] = "both"
                if a.get("last_seen") and (not p.get("last_seen") or a["last_seen"] > p["last_seen"]):
                    p["seen"], p["last_seen"] = a["seen"], a["last_seen"]
                if a.get("is_wired"): p["is_wired"] = True
                p.setdefault("linked", []).append({
                    "mac": aliased_mac, "ip": a["ip"], "network": a["network"], "seen": a["seen"]
                })
                to_remove.add(aliased_mac)
            else:
                # Aliased device not currently visible — still show the link from stored info
                ae = stored.get(aliased_mac, {})
                p.setdefault("linked", []).append({
                    "mac": aliased_mac,
                    "ip": ae.get("ip", ""),
                    "network": ae.get("network", "unknown"),
                    "seen": None,
                })

        return sorted(
            [r for r in result if r["mac"] not in to_remove],
            key=lambda x: (x["unnamed"], str(x["vendor"]).lower(), x["mac"])
        )

    async def update_device(self, mac: str, data: Dict[str, Any]):
        import re
        mac = normalize_mac(mac)
        if not re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", mac):
            raise ValueError("Invalid MAC format")

        async with self.lock:
            stored = await self._load_stored()
            entry = stored.get(mac, {})
            
            if "name" in data: entry["name"] = str(data["name"] or "").strip()[:100]
            if "kind" in data: entry["kind"] = str(data["kind"] or "").strip()[:50]
            if "status" in data: entry["status"] = data["status"]
            if "ip" in data: entry["ip"] = str(data["ip"] or "").strip()[:50]
            if "network" in data: entry["network"] = str(data["network"] or "").strip()[:20]
            
            if "aliases" in data:
                new_aliases = [normalize_mac(str(a)) for a in data["aliases"] if a]
                valid = [a for a in new_aliases if re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", a)][:20]
                for old in entry.get("aliases", []):
                    if old not in valid and old in stored and stored[old].get("primary") == mac:
                        del stored[old]["primary"]
                entry["aliases"] = valid
                for amac in valid:
                    ae = stored.get(amac, {})
                    ae["primary"] = mac
                    stored[amac] = ae
                if "primary" in entry: del entry["primary"]

            if "unlink" in data:
                ulmac = normalize_mac(str(data["unlink"]))
                entry["aliases"] = [a for a in entry.get("aliases", []) if a != ulmac]
                if ulmac in stored and stored[ulmac].get("primary") == mac:
                    del stored[ulmac]["primary"]

            # Reconciliation
            for k, v in list(stored.items()):
                p = v.get("primary")
                if p and (p not in stored or k not in stored[p].get("aliases", [])):
                    del v["primary"]

            if any(v for v in entry.values() if v):
                stored[mac] = entry
            elif mac in stored:
                del stored[mac]

            if not await self._save_stored(stored):
                raise RuntimeError("Failed to save network devices")

network_service = NetworkService()
