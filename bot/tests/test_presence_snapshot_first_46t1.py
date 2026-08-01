"""family-bot-46t.1: snapshot-first presence for web shell APIs."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestPresenceSnapshotFirst46t1(unittest.IsolatedAsyncioTestCase):
    async def test_for_web_returns_cache_without_waiting(self):
        from presence_service import PresenceService

        svc = PresenceService(adapters=[])
        svc._full_presence_cache = {
            "alice": {"home": True, "status_label": "Home", "name": "alice"}
        }
        svc._full_presence_cache_ts = __import__("time").monotonic()  # fresh

        async def slow_full():
            await asyncio.sleep(2)
            return {"alice": {"home": False}}

        svc.get_full_presence = slow_full  # type: ignore
        t0 = __import__("time").monotonic()
        full, source = await svc.get_full_presence_for_web(wait_s=0.35)
        elapsed = __import__("time").monotonic() - t0
        self.assertEqual(source, "cache")
        self.assertTrue(full["alice"]["home"])
        self.assertLess(elapsed, 0.5)

    async def test_timeout_falls_back_to_light(self):
        from presence_service import PresenceService

        svc = PresenceService(adapters=[])
        svc._full_presence_cache = {}
        svc._full_presence_cache_ts = 0.0

        async def slow_full():
            await asyncio.sleep(2)
            return {}

        light = {
            "bob": {
                "home": True,
                "status_label": "Home",
                "name": "bob",
                "snapshot": True,
            }
        }
        svc.get_full_presence = slow_full  # type: ignore
        svc.get_full_presence_light = AsyncMock(return_value=light)
        full, source = await svc.get_full_presence_for_web(wait_s=0.15)
        self.assertEqual(source, "light")
        self.assertTrue(full["bob"].get("snapshot"))
        svc.get_full_presence_light.assert_awaited()

    async def test_fresh_full_updates_cache(self):
        from presence_service import PresenceService

        svc = PresenceService(adapters=[])
        svc._full_presence_cache = {}
        result = {"carol": {"home": False, "status_label": "Away", "name": "carol"}}
        svc.get_full_presence = AsyncMock(return_value=result)
        full, source = await svc.get_full_presence_for_web(wait_s=1.0)
        self.assertEqual(source, "full")
        self.assertEqual(full["carol"]["status_label"], "Away")

    def test_api_routes_use_for_web(self):
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "api" / "routes" / "today.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn("get_full_presence_for_web", text)
        self.assertNotIn(
            "wait_for(presence_service.get_full_presence()",
            text,
        )
        self.assertIn("presence_stale", text)
        self.assertIn("presence_ms=", text)
        self.assertIn('"snapshot"', text)

    async def test_timeout_reuses_inflight_not_second_ha_call(self):
        """On cold timeout, bg schedule must not start a second get_full_presence."""
        from presence_service import PresenceService

        svc = PresenceService(adapters=[])
        calls = {"n": 0}

        async def slow_full():
            calls["n"] += 1
            await asyncio.sleep(2)
            return {"z": {"home": True, "name": "z"}}

        svc.get_full_presence = slow_full  # type: ignore
        svc.get_full_presence_light = AsyncMock(
            return_value={"z": {"home": True, "snapshot": True, "name": "z"}}
        )
        full, source = await svc.get_full_presence_for_web(wait_s=0.1)
        self.assertEqual(source, "light")
        # Only one inflight create — schedule awaits it, doesn't call get_full again
        self.assertEqual(calls["n"], 1)
        self.assertTrue(full["z"].get("snapshot") or full["z"].get("home") is not None)

    def test_client_enrich_hook(self):
        from pathlib import Path
        app = Path(__file__).resolve().parents[2] / "web" / "static" / "js" / "app.v6.js"
        js = app.read_text(encoding="utf-8")
        self.assertIn("maybeSchedulePresenceEnrich", js)
        self.assertIn("presence.enriched", js)
        self.assertIn("presence_stale", js)


if __name__ == "__main__":
    unittest.main()
