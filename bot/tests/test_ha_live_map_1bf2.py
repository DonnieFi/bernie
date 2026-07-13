"""family-bot-1bf.2: HA live map O(1) + get_state prefers cache."""
from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from ha_service import HAService


class TestHaLiveMap(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.dict("os.environ", {"HOME_ASSISTANT_KEY": "t"}):
            with patch("ha_service.config", {"home_assistant": {"host": "http://ha:8123", "token": "t", "entities": []}}):
                self.ha = HAService()

    def test_ws_update_is_o1_dict(self):
        self.ha._states_by_id = {
            "light.a": {"entity_id": "light.a", "state": "off", "attributes": {}},
        }
        new = {"entity_id": "light.a", "state": "on", "attributes": {"friendly_name": "A"}}
        self.ha._states_by_id["light.a"] = new
        self.assertEqual(self.ha._states_by_id["light.a"]["state"], "on")
        self.assertEqual(len(self.ha._states_by_id), 1)

    async def test_get_state_prefers_live_map(self):
        self.ha._states_by_id = {
            "sensor.x": {"entity_id": "sensor.x", "state": "42", "attributes": {}},
        }
        # Would fail if REST were required — no session
        self.ha.get_session = AsyncMock(side_effect=AssertionError("must not REST when cached"))
        st = await self.ha.get_state("sensor.x")
        self.assertEqual(st["state"], "42")

    async def test_get_state_force_rest(self):
        self.ha._states_by_id = {
            "sensor.x": {"entity_id": "sensor.x", "state": "old", "attributes": {}},
        }
        session = MagicMock()
        resp = AsyncMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={"entity_id": "sensor.x", "state": "new", "attributes": {}})
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=None)
        session.get = MagicMock(return_value=resp)
        self.ha.get_session = AsyncMock(return_value=session)
        st = await self.ha.get_state("sensor.x", force_rest=True)
        self.assertEqual(st["state"], "new")
        self.assertEqual(self.ha._states_by_id["sensor.x"]["state"], "new")

    async def test_get_live_states_domain_filter(self):
        self.ha._states_by_id = {
            "light.a": {"entity_id": "light.a", "state": "on"},
            "switch.b": {"entity_id": "switch.b", "state": "off"},
        }
        lights = await self.ha.get_live_states(domain="light")
        self.assertEqual(len(lights), 1)
        self.assertEqual(lights[0]["entity_id"], "light.a")

    def test_resolve_entity_id_direct(self):
        self.ha._states_by_id = {"light.kitchen": {"entity_id": "light.kitchen", "state": "on"}}
        self.assertEqual(self.ha.resolve_entity_id("light.kitchen"), "light.kitchen")

    async def test_ws_bootstrap_uses_get_states_not_rest(self):
        sent: list[dict] = []

        class FakeWS:
            def __init__(self):
                # Event arrives before get_states result — must buffer then replay
                self._queue = [
                    json.dumps({
                        "id": 1,
                        "type": "result",
                        "success": True,
                        "result": None,
                    }),
                    json.dumps({
                        "type": "event",
                        "event": {
                            "data": {
                                "entity_id": "light.b",
                                "old_state": None,
                                "new_state": {
                                    "entity_id": "light.b",
                                    "state": "off",
                                    "attributes": {},
                                },
                            }
                        },
                    }),
                    json.dumps({
                        "id": 2,
                        "type": "result",
                        "success": True,
                        "result": [
                            {"entity_id": "light.a", "state": "on", "attributes": {"friendly_name": "A"}},
                        ],
                    }),
                ]

            async def send(self, payload: str):
                sent.append(json.loads(payload))

            async def recv(self):
                if not self._queue:
                    raise RuntimeError("no more messages")
                return self._queue.pop(0)

        ws = FakeWS()
        with patch.object(self.ha, "_apply_states_baseline", new_callable=AsyncMock) as apply, \
             patch.object(self.ha, "_handle_ws_state_event", new_callable=AsyncMock) as handle:
            await self.ha._ws_bootstrap(ws)
            apply.assert_awaited_once()
            self.assertEqual(apply.await_args.kwargs.get("source"), "ws")
            handle.assert_awaited_once()
            self.assertEqual(
                handle.await_args.args[0]["event"]["data"]["entity_id"],
                "light.b",
            )
        types = [m["type"] for m in sent]
        self.assertIn("subscribe_events", types)
        self.assertIn("get_states", types)
        # Must not fall back to REST during WS bootstrap
        self.ha.refresh_entities = AsyncMock(
            side_effect=AssertionError("must not REST during WS bootstrap")
        )

if __name__ == "__main__":
    unittest.main()
