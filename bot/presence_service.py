import asyncio
import aiohttp
import logging
import os
import time
from datetime import datetime, timezone
from config import config
from db_binding import get_database
from constants import resolve_person_from_entity, is_family_member
import db_writes

log = logging.getLogger(__name__)

GPS_STALE_SECONDS = 20 * 60  # device_tracker ping older than this → GPS "home" treated as unknown

class PresenceService:
    def __init__(self, adapters=None):
        self.presence_cfg = config.get("presence", {})
        self.unifi_host = self.presence_cfg.get("unifi_host", "https://192.168.1.X")  # default gateway placeholder; configure presence.unifi_host
        self.unifi_key = os.environ.get("UNIFI_KEY")
        self.polling_interval = self.presence_cfg.get("polling_interval_seconds", 300)  # 5 minutes instead of 60s (real-time WS events now drive presence)
        self.is_running = False
        self._task = None
        self.arrival_callbacks = []
        self.departure_callbacks = []
        self.friend_arrival_callbacks = []
        self._last_states: dict[str, dict] = {}
        if adapters is not None:
            self._adapters = adapters
        else:
            from presence.adapters import HANetworkPresenceAdapter, UniFiPresenceAdapter

            self._adapters = [
                UniFiPresenceAdapter(self.unifi_host, self.unifi_key),
                HANetworkPresenceAdapter(config),
            ]

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._task = asyncio.create_task(self._poll_loop())
        log.info("Presence service started.")

    async def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Presence service stopped.")

    async def _poll_loop(self):
        while self.is_running:
            try:
                await self.check_presence()
            except Exception as e:
                log.error(f"Error checking presence: {e}")
            await asyncio.sleep(self.polling_interval)

    def on_arrive(self, callback):
        self.arrival_callbacks.append(callback)

    def on_friend_arrive(self, callback):
        self.friend_arrival_callbacks.append(callback)

    def on_depart(self, callback):
        self.departure_callbacks.append(callback)

    def _compute_status_label(self, is_home: bool, zone: str) -> str:
        """Centralized logic for the pretty status label."""
        if is_home:
            return "Home"
        if not zone or zone in ("away", "not_home", "unknown", "home"):
            return "Away"
        return zone.title()

    async def check_presence(self, force_refresh: bool = False):
        from constants import registry
        family_members = config.get("family_members", {})

        # WiFi signals via injected presence adapters (UniFi + HA scanner)
        active_macs: dict[str, str] = {}
        active_ips: dict[str, str] = {}
        if not self.unifi_key:
            log.warning("UNIFI_KEY not set. Skipping Unifi presence check.")
        # family-bot-ah5.4: fetch adapters concurrently
        async def _fetch_one(adapter):
            try:
                return await adapter.fetch_active_clients()
            except Exception as e:
                log.warning("Presence adapter %s failed: %s", type(adapter).__name__, e)
                return {}

        adapter_results = await asyncio.gather(
            *(_fetch_one(a) for a in self._adapters)
        ) if self._adapters else []
        for clients in adapter_results:
            for mac, info in (clients or {}).items():
                active_macs[mac.lower()] = info.get("essid") or "unknown"
                if info.get("ip"):
                    active_ips[info["ip"]] = info.get("essid") or "unknown"

        member_wifi: dict[str, bool | None] = {}
        member_mac: dict[str, str | None] = {}
        member_essid: dict[str, str | None] = {}

        for display_name, info in family_members.items():
            cid = info.get("canonical_id") or registry.resolve(display_name) or display_name.lower()
            macs = info.get("device_macs", [])
            device_ip = info.get("device_ip")

            found_mac = next((mac for mac in macs if mac.lower() in active_macs), None)
            found_essid = active_macs.get(found_mac.lower()) if found_mac else None

            if not found_mac and device_ip and device_ip in active_ips:
                found_essid = active_ips[device_ip]
                log.debug(f"Detected {display_name} by IP {device_ip} (MAC not matched)")

            if found_mac:
                log.debug(f"Detected MAC {found_mac} for {display_name}")

            is_present = (found_mac is not None) or (found_essid is not None)
            member_mac[cid] = found_mac
            member_wifi[cid] = is_present if (macs or device_ip) else None
            member_essid[cid] = found_essid

        # Log unknown MACs and announce friend arrivals — fire-and-forget so
        # DB reads don't block the presence poll on every cycle.
        from identity_service import identity_service
        known_macs = {
            mac.lower()
            for info in family_members.values()
            for mac in info.get("device_macs", [])
        }
        unknown_macs = {mac: essid for mac, essid in active_macs.items() if mac not in known_macs}
        if unknown_macs:
            async def _process_unknown_macs(macs: dict):
                for mac, essid in macs.items():
                    resolved = await identity_service.resolve_entity(mac)
                    if resolved:
                        node = await identity_service.get_identity(resolved["canonical_id"])
                        if node and node.get("metadata", {}).get("role") == "friend":
                            label = node["metadata"].get("display", resolved["canonical_id"])
                            log.info(f"Friend arrived: {label} ({mac})")
                            for cb in self.friend_arrival_callbacks:
                                asyncio.create_task(cb(label, mac))
                    else:
                        await identity_service.log_unresolved_entity(mac, "mac", {"essid": essid})
            asyncio.create_task(_process_unknown_macs(unknown_macs))

        # GPS Signals
        needs_ha = any(info.get("ha_entity") for info in family_members.values())
        ha_states = await self._fetch_ha_person_states() if (needs_ha or force_refresh) else {}

        now_ts = time.time()
        new_states = {}

        # Precompute per-member home flags, then one DB transaction (ah5.4)
        pending: list[dict] = []
        meta: list[tuple[str, str, bool | None, bool | None, str]] = []
        # (display_name, cid, wifi_home, ha_home, status_label after compute)

        for display_name, info in family_members.items():
            cid = info.get("canonical_id") or registry.resolve(display_name) or display_name.lower()
            ha_entity = info.get("ha_entity")
            wifi_home = member_wifi.get(cid)
            found_mac = member_mac.get(cid)

            ha_home: bool | None = None
            if ha_entity and ha_states:
                ha_state_info = ha_states.get(ha_entity, {})
                ha_state = ha_state_info.get("state")
                if ha_state == "home":
                    ha_home = True
                    # Check device_tracker freshness. The person entity only updates
                    # last_updated on zone transitions, so we check the tracker which
                    # updates on every location ping.
                    tracker_entity = (
                        config.get("presence", {})
                        .get("device_trackers", {})
                        .get(cid, {})
                        .get("device_tracker")
                    )
                    if tracker_entity and tracker_entity in ha_states:
                        lu = ha_states[tracker_entity].get("last_updated")
                        if lu:
                            try:
                                age = now_ts - datetime.fromisoformat(lu).timestamp()
                                if age > GPS_STALE_SECONDS:
                                    log.info(
                                        f"GPS 'home' for {display_name} is {age/60:.0f}m stale "
                                        f"({tracker_entity}) — treating as unknown"
                                    )
                                    ha_home = None
                            except Exception:
                                pass
                elif ha_state in ("not_home", "away"):
                    ha_home = False

            # Build state keyed by canonical_id
            ha_zone = ha_states.get(ha_entity, {}).get("state", "unknown") if ha_entity else "unknown"
            if ha_zone == "not_home": ha_zone = "away"

            new_states[cid] = {
                "wifi": wifi_home,
                "gps": ha_home,
                "zone": ha_zone,
                "essid": member_essid.get(cid)
            }

            raw_is_home = (wifi_home is True) or (ha_home is True)
            pending.append({
                "person_id": cid,
                "is_home": raw_is_home,  # tentative; grace applied after batch signal read
                "device_mac": found_mac,
                "set_last_home_signal": now_ts if raw_is_home else None,
                "raw_is_home": raw_is_home,
                "display_name": display_name,
                "wifi_home": wifi_home,
                "ha_home": ha_home,
                "ha_zone": ha_zone,
            })

        # Grace period: if not raw_home, stay home if last_home_signal within 120s
        cids = [p["person_id"] for p in pending]
        last_signals = await get_database().get_last_home_signals(cids)
        for p in pending:
            if p["raw_is_home"]:
                p["is_home"] = True
            else:
                last_seen_ts = last_signals.get(p["person_id"]) or 0
                p["is_home"] = (now_ts - last_seen_ts) < 120

        # One transaction for all presence writes
        results = await db_writes.apply_presence_tick(
            [
                {
                    "person_id": p["person_id"],
                    "is_home": p["is_home"],
                    "device_mac": p["device_mac"],
                    "set_last_home_signal": p["set_last_home_signal"],
                }
                for p in pending
            ]
        )
        by_id = {pid: changed for pid, changed in (results or [])}
        for p in pending:
            cid = p["person_id"]
            is_home = p["is_home"]
            status_label = self._compute_status_label(is_home, p["ha_zone"])
            if by_id.get(cid):
                status = "arrived" if is_home else "departed"
                log.info(
                    f"Presence change: {p['display_name']} ({cid}) {status} "
                    f"(label={status_label}, wifi={p['wifi_home']}, gps={p['ha_home']})"
                )
                if is_home:
                    for cb in self.arrival_callbacks:
                        await cb(cid)
                else:
                    for cb in self.departure_callbacks:
                        await cb(cid, status_label=status_label)

        # Atomic update of the cache at the end to prevent race conditions during async loop
        self._last_states = new_states
    async def _fetch_ha_person_states(self) -> dict[str, dict]:
        from ha_service import ha_service
        try:
            # WebSocket keeps _live_states current; no need to re-fetch all 550 entities here
            states = await ha_service.get_live_states(domain="person")
            trackers = await ha_service.get_live_states(domain="device_tracker")
            result = {}
            for s in states + trackers:
                eid = s.get("entity_id")
                if eid:
                    result[eid] = {"state": s.get("state"), "last_updated": s.get("last_updated")}
            return result
        except Exception as e:
            log.error(f"Failed to fetch HA person states: {e}")
            return {}

    async def get_presence(self):
        return await get_database().get_presence()

    async def is_any_home(self, person_ids: list[str]) -> bool:
        """True if any person_id is home in the presence DB (family-bot-1bf.4).

        Lightweight path for gates (Frigate away) — no HA location REST fan-out.
        ``is_home`` is maintained by check_presence / WS (staleness already applied).
        """
        if not person_ids:
            return False
        db_presence = await get_database().get_presence()
        for pid in person_ids:
            if db_presence.get(pid, {}).get("is_home"):
                return True
        return False

    async def refresh_presence(self):
        """Manually trigger a full presence check."""
        await self.check_presence(force_refresh=True)
        return await self.get_full_presence()

    async def get_full_presence(self) -> dict:
        import time
        from ha_service import ha_service
        from constants import registry

        now_ts = time.time()
        db_presence = await get_database().get_presence()
        ha_locations = await ha_service.get_all_person_locations()
        ha_by_id = {loc["person_id"]: loc for loc in ha_locations}

        result = {}
        family_members = config.get("family_members", {})
        for display_name, member in family_members.items():
            if member.get("role") == "friend":
                continue
            cid = member.get("canonical_id") or registry.resolve(display_name) or display_name.lower()
            if not cid:
                continue

            db = db_presence.get(cid, {})
            ha = ha_by_id.get(cid, {})

            zone = ha.get("state", "unknown")
            if zone == "not_home":
                zone = "away"

            is_home = db.get("is_home", False)
            departing = False

            conflict_label = None
            raw_states = self._last_states.get(cid, {})

            wifi = raw_states.get("wifi")
            gps = raw_states.get("gps")
            essid = raw_states.get("essid")

            if is_home and wifi is False and gps is False:
                last_seen_ts = await get_database().get_last_home_signal(cid) or 0
                if now_ts - last_seen_ts < 120:
                    departing = True
            
            if is_home:
                if wifi is True and gps is False:
                    conflict_label = "Ghost in the machine (WiFi present, GPS lost)"
                elif gps is True and wifi is False:
                    conflict_label = "Left phone behind (GPS home, WiFi gone)"

            # Status Label: "Home", "Away", or specific HA Geofence
            status_label = self._compute_status_label(is_home, zone)

            result[cid] = {
                "name": cid,
                "display": display_name,
                "home": is_home,
                "status_label": status_label,
                "departing": departing,
                "wifi": wifi is True,
                "essid": essid,
                "zone": zone,
                "gps": {
                    "lat": ha.get("latitude"),
                    "lon": ha.get("longitude"),
                    "accuracy": ha.get("gps_accuracy")
                } if ha.get("latitude") else None,
                "gps_updated": ha.get("gps_last_updated"),
                "address": ha.get("address"),
                "battery": ha.get("battery"),
                "last_seen": db.get("last_seen"),
                "conflict_label": conflict_label
            }

        return result

    async def _on_person_state_change(
        self, entity_id: str, new_state: dict, old_state: dict | None = None
    ):
        """Called in real-time by HA WebSocket when a person.* or device_tracker.* entity changes."""
        try:
            if not is_family_member(entity_id):
                return

            new_zone = new_state.get("state")
            old_zone = old_state.get("state") if old_state else None
            if old_zone == new_zone:
                log.debug(
                    "WebSocket presence attribute refresh (zone unchanged): %s → %s",
                    entity_id,
                    new_zone,
                )
                return

            log.info(f"WebSocket presence event: {entity_id} → {new_zone}")

            # Force immediate full presence recalculation
            await self.check_presence(force_refresh=True)

        except Exception as e:
            log.error(f"Error handling person state change for {entity_id}: {e}")


presence_service = PresenceService()
