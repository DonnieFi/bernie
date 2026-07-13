import os
import asyncio
import json
import logging
import time
import aiohttp
import websockets
from urllib.parse import urlparse
from config import config
import db_writes

log = logging.getLogger(__name__)

_temp_history_cache: dict[str, tuple[float, list]] = {}
_TEMP_HISTORY_TTL = 3600  # 1 hour — graph data doesn't need sub-hour freshness


class HAService:
    def __init__(self):
        ha_config = config.get("home_assistant", {})
        self.host = ha_config.get("host", "http://homeassistant.local:8123")
        self.host_ip = urlparse(self.host).hostname or "homeassistant.local"
        # Config whitelist still used for UI room grouping/labels
        self._config_entities = {e["entity_id"]: e for e in ha_config.get("entities", [])}
        self.automations = {a["entity_id"]: a for a in (ha_config.get("automations") or [])}
        self._broadcaster = None
        self._ws_task = None
        self._session = None

        # Live registry — entity_id → state dict (family-bot-1bf.2: O(1) WS updates)
        self._states_by_id: dict[str, dict] = {}
        self._name_map: dict[str, str] = {}  # friendly_name.lower() → entity_id

        self.token = ha_config.get("token", os.environ.get("HOME_ASSISTANT_KEY"))
        if not self.token:
            log.warning("HOME_ASSISTANT_KEY not set. HA integrations will fail.")

        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    async def _apply_states_baseline(self, states: list[dict], *, source: str = "rest") -> None:
        """Rebuild live registry from a full state list (WS get_states or REST)."""

        def _build_name_map():
            name_map = {}
            for s in states:
                friendly = s.get("attributes", {}).get("friendly_name", "")
                if friendly:
                    name_map[friendly.lower()] = s.get("entity_id", "")
            return name_map

        new_name_map = await asyncio.to_thread(_build_name_map)
        self._states_by_id = {
            s["entity_id"]: s for s in states if s.get("entity_id")
        }
        self._name_map = new_name_map
        log.info(
            "HA entity registry refreshed (%s) — %s entities, %s named",
            source,
            len(self._states_by_id),
            len(self._name_map),
        )
        asyncio.create_task(db_writes.routed("save_ha_devices", states))

    async def refresh_entities(self):
        """Fetch all states from HA REST (fallback when WS is down)."""
        url = f"{self.host}/api/states"
        session = await self.get_session()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    states = await resp.json()
                    await self._apply_states_baseline(states, source="rest")
                else:
                    log.error(f"HA refresh_entities error: {resp.status} — keeping last known state")
                    await self._load_from_db_cache()
        except Exception as e:
            log.error(f"HA refresh_entities failed: {e} — keeping last known state")
            await self._load_from_db_cache()

    async def _load_from_db_cache(self) -> bool:
        """Fall back to last DB snapshot when HA is unreachable. Returns True if data loaded."""
        if self._states_by_id:
            return False  # already have live data — don't overwrite
        try:
            from db_binding import get_database
            cached = await get_database().get_ha_devices()
            if not cached:
                return False
            # Reconstruct minimal HA state objects from DB rows
            rebuilt: dict[str, dict] = {}
            for d in cached:
                eid = d["entity_id"]
                rebuilt[eid] = {
                    "entity_id": eid,
                    "state": d["last_state"] or "unknown",
                    "attributes": {"friendly_name": d["name"]},
                    "last_updated": d["last_updated"],
                }
                if d["name"] and d["name"] != eid:
                    self._name_map[d["name"].lower()] = eid
            self._states_by_id = rebuilt
            log.info("HA unreachable — loaded %s entities from DB cache", len(rebuilt))
            return True
        except Exception as e:
            log.warning(f"HA DB cache fallback failed: {e}")
            return False

    def resolve_entity_id(self, name: str) -> str | None:
        """Resolve a friendly name or partial name to an entity_id from the live registry."""
        name_lower = name.lower().strip()
        # 1. Exact friendly name match
        if name_lower in self._name_map:
            return self._name_map[name_lower]
        # 2. Direct entity_id match (O(1) on live map)
        if name_lower in self._states_by_id:
            return name_lower
        for eid in self._states_by_id:
            if eid.lower() == name_lower:
                return eid
        # 3. Partial friendly name match
        for friendly, eid in self._name_map.items():
            if name_lower in friendly:
                return eid
        # 4. Partial entity_id slug match (e.g. "garmin" hits "sensor.garmin_sleep_score")
        for eid in self._states_by_id:
            slug = eid.split(".", 1)[-1].lower()
            if name_lower in slug:
                return eid
        # 5. Word-overlap: all query words appear somewhere in entity_id or friendly_name.
        words = name_lower.split()
        if len(words) > 1:
            _domain_priority = ["light", "switch", "climate", "cover", "media_player",
                                 "sensor", "binary_sensor", "input_boolean", "automation"]
            matches = []
            for eid, s in self._states_by_id.items():
                friendly = s.get("attributes", {}).get("friendly_name", "").lower()
                target = f"{eid.lower()} {friendly}"
                if all(w in target for w in words):
                    domain = eid.split(".")[0]
                    priority = _domain_priority.index(domain) if domain in _domain_priority else len(_domain_priority)
                    matches.append((priority, eid))
            if matches:
                return min(matches, key=lambda x: x[0])[1]
        return None

    async def get_live_states(self, domain: str | None = None) -> list[dict]:
        """Return live states, optionally filtered by domain prefix."""
        if not self._states_by_id:
            await self.refresh_entities()
        if domain:
            prefix = f"{domain}."
            return [s for eid, s in self._states_by_id.items() if eid.startswith(prefix)]
        return list(self._states_by_id.values())

    async def get_state(self, entity_id: str, *, force_rest: bool = False) -> dict:
        """Return entity state; prefer live WS/refresh map (family-bot-1bf.2).

        Falls back to REST when map is cold or force_rest=True.
        """
        if not force_rest:
            cached = self._states_by_id.get(entity_id)
            if cached is not None:
                return cached
        url = f"{self.host}/api/states/{entity_id}"
        session = await self.get_session()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    state = await resp.json()
                    eid = state.get("entity_id") or entity_id
                    self._states_by_id[eid] = state
                    return state
                log.error(f"HA get_state error: {resp.status} for {entity_id} - {await resp.text()}")
                return {}
        except Exception as e:
            log.error(f"HA connection error: {e}")
            return {}

    async def get_all_states(self) -> list[dict]:
        """Get states for all config-listed entities (for room grouping in UI)."""
        if self._states_by_id:
            ids = set(self._config_entities.keys())
            return [self._states_by_id[i] for i in ids if i in self._states_by_id]
        # Fallback before first refresh — REST per config entity
        states = []
        for entity_id in self._config_entities:
            state = await self.get_state(entity_id, force_rest=True)
            if state:
                states.append(state)
        return states

    async def conversation_process(self, text: str, *, language: str = "en") -> dict:
        """POST /api/conversation/process (HA Assist) — family-bot-5hy.13."""
        session = await self.get_session()
        url = f"{self.host}/api/conversation/process"
        payload = {"text": text, "language": language}
        try:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    return {
                        "ok": False,
                        "status": resp.status,
                        "error": body if isinstance(body, dict) else str(body),
                    }
                return {"ok": True, "result": body}
        except Exception as e:
            log.error("HA conversation/process failed: %s", e)
            return {"ok": False, "error": str(e)}

    async def get_person_location(self, person_id: str) -> dict:
        """Get state, GPS, and battery for a tracked family member."""
        trackers = config.get("presence", {}).get("device_trackers", {})
        person_cfg = trackers.get(person_id.lower())
        if not person_cfg:
            return {"error": f"No tracker configured for {person_id}"}

        result: dict = {"person_id": person_id}

        # Prioritise device_tracker (e.g. iCloud3) over person_entity
        main_entity = person_cfg.get("device_tracker") or person_cfg.get("person_entity")
        
        if main_entity:
            state = await self.get_state(main_entity)
            attrs = state.get("attributes", {})
            result["state"] = state.get("state", "unknown")
            result["latitude"] = attrs.get("latitude")
            result["longitude"] = attrs.get("longitude")
            result["gps_accuracy"] = attrs.get("gps_accuracy")
            result["gps_last_updated"] = state.get("last_updated")
            result["address"] = attrs.get("address")

        battery_entity = person_cfg.get("battery_sensor")
        if battery_entity:
            batt = await self.get_state(battery_entity)
            result["battery"] = batt.get("state")
        else:
            result["battery"] = None

        return result

    async def get_all_person_locations(self) -> list[dict]:
        """Get location for all configured tracked persons."""
        trackers = config.get("presence", {}).get("device_trackers", {})
        if not trackers:
            return []
        results = await asyncio.gather(
            *[self.get_person_location(pid) for pid in trackers],
            return_exceptions=True
        )
        return [r for r in results if isinstance(r, dict)]

    async def get_temperature_sensors(self) -> list[dict]:
        """Return all temperature sensor states from the live registry."""
        live = await self.get_live_states(domain="sensor")
        return [
            s for s in live
            if "temperature" in s.get("entity_id", "").lower()
            or "temperature" in s.get("attributes", {}).get("unit_of_measurement", "").lower()
            or s.get("attributes", {}).get("device_class") == "temperature"
        ]

    async def get_temperature_history(self, entity_id: str, hours: int = 24) -> list[dict]:
        """Fetch last N hours of readings from HA history API. Cached for 1 hour."""
        cache_key = f"{entity_id}:{hours}"
        entry = _temp_history_cache.get(cache_key)
        if entry and (time.monotonic() - entry[0]) < _TEMP_HISTORY_TTL:
            return entry[1]

        from datetime import datetime, timezone, timedelta
        start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        url = f"{self.host}/api/history/period/{start}"
        params = {"filter_entity_id": entity_id, "minimal_response": "true"}
        session = await self.get_session()
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = data[0] if data and isinstance(data[0], list) else []
                    _temp_history_cache[cache_key] = (time.monotonic(), result)
                    return result
                log.error(f"HA history error: {resp.status}")
                return []
        except Exception as e:
            log.error(f"HA get_temperature_history error: {e}")
            return []

    async def get_history_batch(self, entity_ids: list[str], hours: int = 24) -> dict[str, list[dict]]:
        """Fetch last N hours of readings for multiple entities in one HA API call."""
        results = {}
        missing = []
        for eid in entity_ids:
            cache_key = f"{eid}:{hours}"
            entry = _temp_history_cache.get(cache_key)
            if entry and (time.monotonic() - entry[0]) < _TEMP_HISTORY_TTL:
                results[eid] = entry[1]
            else:
                missing.append(eid)
        
        if not missing:
            return results
            
        from datetime import datetime, timezone, timedelta
        start = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        url = f"{self.host}/api/history/period/{start}"
        params = {"filter_entity_id": ",".join(missing), "minimal_response": "true"}
        session = await self.get_session()
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # HA returns a list of lists, where each inner list is history for one entity
                    for entity_history in data:
                        if entity_history and isinstance(entity_history, list):
                            eid = entity_history[0].get("entity_id")
                            if eid:
                                _temp_history_cache[f"{eid}:{hours}"] = (time.monotonic(), entity_history)
                                results[eid] = entity_history
                else:
                    log.error(f"HA history batch error: {resp.status}")
        except Exception as e:
            log.error(f"HA get_history_batch connection error: {e}")

        # Fallback for any still-missing entities
        final_missing = [eid for eid in entity_ids if eid not in results]
        if final_missing:
            log.info(f"Fetching history individually for {len(final_missing)} entities as fallback")
            fallback_histories = await asyncio.gather(
                *[self.get_temperature_history(eid, hours=hours) for eid in final_missing],
                return_exceptions=True
            )
            for eid, history in zip(final_missing, fallback_histories):
                if isinstance(history, list):
                    results[eid] = history
                else:
                    log.error(f"Fallback history fetch failed for {eid}: {history}")

        return results

    async def _call_service(self, domain: str, service: str, entity_id: str, attrs: dict | None = None) -> bool:
        url = f"{self.host}/api/services/{domain}/{service}"
        payload = {"entity_id": entity_id}
        if attrs:
            payload.update(attrs)
        session = await self.get_session()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True
                log.error(f"HA _call_service error: {resp.status} - {await resp.text()}")
                return False
        except Exception as e:
            log.error(f"HA connection error: {e}")
            return False

    async def _broadcast_light_state(self, entity_id: str, is_on: bool):
        if self._broadcaster:
            slug = entity_id.split(".")[-1].replace("_", "-")
            await self._broadcaster({
                "type": "light.state",
                "id": slug,
                "on": is_on,
                "last": f"{'on' if is_on else 'off'} · just now",
            })

    async def turn_on(self, entity_id: str) -> bool:
        domain = entity_id.split(".")[0]
        ok = await self._call_service(domain, "turn_on", entity_id)
        if ok:
            await self._broadcast_light_state(entity_id, True)
        return ok

    async def turn_off(self, entity_id: str) -> bool:
        domain = entity_id.split(".")[0]
        ok = await self._call_service(domain, "turn_off", entity_id)
        if ok:
            await self._broadcast_light_state(entity_id, False)
        return ok

    async def set_light_state(self, entity_id: str, on: bool,
                               brightness: int | None = None,
                               color_temp: int | None = None,
                               rgb: list[int] | None = None) -> bool:
        if not on:
            return await self.turn_off(entity_id)
        attrs: dict = {}
        if brightness is not None:
            attrs["brightness"] = max(0, min(255, brightness))
        if color_temp is not None:
            attrs["color_temp_kelvin"] = color_temp
        if rgb is not None:
            attrs["rgb_color"] = rgb
        ok = await self._call_service("light", "turn_on", entity_id, attrs)
        if ok:
            await self._broadcast_light_state(entity_id, True)
        return ok

    async def toggle(self, entity_id: str) -> bool:
        domain = entity_id.split(".")[0]
        ok = await self._call_service(domain, "toggle", entity_id)
        if ok:
            state = await self.get_state(entity_id)
            if state:
                await self._broadcast_light_state(entity_id, state.get("state") == "on")
        return ok

    def set_broadcaster(self, broadcaster):
        self._broadcaster = broadcaster

    async def trigger_automation(self, automation_id: str) -> bool:
        domain = automation_id.split(".")[0]
        if domain != "automation":
            log.warning(f"Attempted to trigger non-automation: {automation_id}")
            return False

        if self.automations and automation_id not in self.automations:
            log.warning(f"Unauthorized automation: {automation_id}")
            return False

        return await self._call_service(domain, "trigger", automation_id)

    async def play_media(
        self,
        entity_id: str,
        media_content_id: str,
        media_content_type: str = "music",
        volume: float | None = None,
    ) -> bool:
        """Play media on a media_player entity."""
        if volume is not None:
            clamped = max(0.0, min(1.0, float(volume)))
            await self._call_service(
                "media_player",
                "volume_set",
                entity_id,
                {"volume_level": clamped},
            )
        return await self._call_service(
            "media_player",
            "play_media",
            entity_id,
            {"media_content_id": media_content_id, "media_content_type": media_content_type},
        )

    # tts_announce REMOVED (GitHub readiness audit): no hardware TTS path existed.
    # The announce_on_speaker tool was unregistered from the tool surface.
    # If real Sonos/tts.speak support is added later, reintroduce a working impl here
    # and the matching @tool in tools/media.py.
    # Current media players (see config home_assistant.entities) support play_media only.

    async def media_control(self, entity_id: str, command: str, volume: float | None = None) -> bool:
        """Send a playback control command to a media_player entity."""
        service_map = {
            "play": "media_play",
            "pause": "media_pause",
            "stop": "media_stop",
            "next": "media_next_track",
            "previous": "media_previous_track",
        }

        if command == "volume_set":
            if volume is None:
                log.warning("media_control: volume_set requires a volume value")
                return False
            clamped = max(0.0, min(1.0, float(volume)))
            return await self._call_service("media_player", "volume_set", entity_id, {"volume_level": clamped})

        if command not in service_map:
            log.warning(f"media_control: unknown command {command!r}")
            return False

        if volume is not None:
            clamped = max(0.0, min(1.0, float(volume)))
            await self._call_service("media_player", "volume_set", entity_id, {"volume_level": clamped})

        return await self._call_service("media_player", service_map[command], entity_id)

    async def _on_person_state_change(self, entity_id, new_state, old_state=None):
        """Hook for presence_service to react to person/device_tracker changes.

        Overridden at runtime by the real implementation in PresenceService
        (wired in main.py for discord/cognition roles).
        Default is a no-op so the method always exists.
        """
        pass  # real handler injected by main._common_setup

    async def _handle_ws_state_event(self, msg: dict) -> None:
        """Apply one state_changed event to the live map."""
        data = msg.get("event", {}).get("data", {})
        entity_id = data.get("entity_id")
        old_state = data.get("old_state")
        new_state = data.get("new_state")

        if not entity_id or not new_state:
            return

        self._states_by_id[entity_id] = new_state
        friendly = (new_state.get("attributes") or {}).get("friendly_name")
        if friendly:
            self._name_map[str(friendly).lower()] = entity_id

        if entity_id.startswith(("person.", "device_tracker.")):
            await self._on_person_state_change(entity_id, new_state, old_state)

    async def _ws_bootstrap(self, ws) -> None:
        """Subscribe + ordered WS get_states baseline (family-bot-ykf)."""
        await ws.send(json.dumps({
            "id": 1,
            "type": "subscribe_events",
            "event_type": "state_changed",
        }))
        await ws.send(json.dumps({"id": 2, "type": "get_states"}))

        baseline_applied = False
        pending_events: list[dict] = []

        while not baseline_applied:
            raw = await ws.recv()
            msg = json.loads(raw)
            mtype = msg.get("type")
            mid = msg.get("id")

            if mtype == "result" and mid == 1:
                if not msg.get("success"):
                    raise RuntimeError(f"HA subscribe_events failed: {msg}")
                continue
            if mtype == "result" and mid == 2:
                if not msg.get("success"):
                    raise RuntimeError(f"HA get_states failed: {msg}")
                states = msg.get("result") or []
                await self._apply_states_baseline(states, source="ws")
                baseline_applied = True
                for ev in pending_events:
                    try:
                        await self._handle_ws_state_event(ev)
                    except Exception as e:
                        log.debug(f"Error replaying WS event: {e}")
                continue
            if mtype == "event":
                pending_events.append(msg)

    async def _ws_loop(self):
        """Main HA WebSocket loop with correct auth handshake and reconnection."""
        backoff = [2, 5, 10, 30, 60]
        attempt = 0

        while True:
            try:
                ws_url = f"ws://{self.host_ip}:8123/api/websocket"
                log.info(f"Connecting to HA WebSocket: {ws_url}")

                async with websockets.connect(ws_url) as ws:
                    attempt = 0
                    log.info("✅ HA WebSocket connected successfully")

                    # Step 1: Receive the auth_required message from HA
                    auth_required = json.loads(await ws.recv())
                    if auth_required.get("type") != "auth_required":
                        # Raise so outer except runs backoff + reconnect (do not break forever)
                        raise RuntimeError(f"Unexpected first message from HA: {auth_required}")

                    log.info("Received auth_required from HA — sending token")

                    # Step 2: Send authentication
                    await ws.send(json.dumps({
                        "type": "auth",
                        "access_token": self.token
                    }))

                    # Step 3: Receive auth_ok or auth_invalid
                    auth_result = json.loads(await ws.recv())
                    if auth_result.get("type") != "auth_ok":
                        raise RuntimeError(f"HA WebSocket authentication failed: {auth_result}")

                    log.info("✅ HA WebSocket authenticated successfully")

                    # Ordered baseline via WS get_states (not REST race)
                    await self._ws_bootstrap(ws)

                    # Process live events
                    async for message in ws:
                        try:
                            msg = json.loads(message)
                            if msg.get("type") != "event":
                                continue
                            await self._handle_ws_state_event(msg)
                        except Exception as e:
                            log.debug(f"Error processing WS message: {e}")

            except Exception as e:
                delay = backoff[min(attempt, len(backoff)-1)]
                log.warning(f"HA WebSocket disconnected: {e} — reconnecting in {delay}s (attempt {attempt+1})")
                attempt += 1
                await asyncio.sleep(delay)

    async def start_websocket(self):
        """Start the persistent HA WebSocket connection."""
        if self._ws_task and not self._ws_task.done():
            log.info("HA WebSocket is already running")
            return
        self._ws_task = asyncio.create_task(self._ws_loop())
        log.info("HA WebSocket task started (real-time events enabled)")

    async def close(self):
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        log.info("HAService closed")

ha_service = HAService()
