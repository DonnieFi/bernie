import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Fixed "now" for deterministic staleness math in get_home_health.
_FIXED_NOW_TS = datetime(2026, 5, 31, 18, 0, 0, tzinfo=timezone.utc).timestamp()


class TestHomeTools(unittest.IsolatedAsyncioTestCase):
    async def test_get_home_health_no_stale_devices(self):
        from tools.home import handle_get_home_health
        
        db_mock = MagicMock()
        db_mock.get_stale_ha_devices = AsyncMock(return_value=[])
        
        ha_mock = MagicMock()
        ha_mock.get_live_states = AsyncMock(return_value=[])
        
        class Ctx:
            class services:
                db = db_mock
                ha = ha_mock
            config = {}
            person_id = "person:red"
            group = "parents"
            shadow = False
            
        ctx = Ctx()
        res = await handle_get_home_health({"stale_minutes": 60}, ctx)
        self.assertIn("All tracked devices reported", res)
        db_mock.get_stale_ha_devices.assert_called_once_with(60)

    async def test_get_home_health_with_stale_devices(self):
        from tools.home import handle_get_home_health
        
        db_mock = MagicMock()
        stale_devices = [
            {"entity_id": "light.living_room", "name": "Living Room Light", "last_updated": "2026-05-31T10:00:00Z"}
        ]
        db_mock.get_stale_ha_devices = AsyncMock(return_value=stale_devices)
        
        ha_mock = MagicMock()
        ha_mock.get_live_states = AsyncMock(return_value=[
            {"entity_id": "light.living_room", "last_updated": "2026-05-31T10:00:00Z"}
        ])
        
        class Ctx:
            class services:
                db = db_mock
                ha = ha_mock
            config = {}
            person_id = "person:red"
            group = "parents"
            shadow = False
            
        ctx = Ctx()
        with patch("tools.home.time.time", return_value=_FIXED_NOW_TS):
            res = await handle_get_home_health({"stale_minutes": 60}, ctx)
        self.assertIn("1 device(s) stale (>60 min)", res)
        self.assertIn("light.living_room", res)

    async def test_get_home_health_live_ha_fresh_filters_db_stale(self):
        """DB marks stale but in-memory HA state is recent — device is excluded."""
        from tools.home import handle_get_home_health

        db_mock = MagicMock()
        db_mock.get_stale_ha_devices = AsyncMock(return_value=[
            {
                "entity_id": "light.living_room",
                "name": "Living Room Light",
                "last_updated": "2026-05-31T10:00:00Z",
            }
        ])

        ha_mock = MagicMock()
        ha_mock.get_live_states = AsyncMock(return_value=[
            {
                "entity_id": "light.living_room",
                "last_updated": "2026-05-31T17:58:00Z",
            }
        ])

        class Ctx:
            class services:
                db = db_mock
                ha = ha_mock
            config = {}
            person_id = "person:red"
            group = "parents"
            shadow = False

        ctx = Ctx()
        with patch("tools.home.time.time", return_value=_FIXED_NOW_TS):
            res = await handle_get_home_health({"stale_minutes": 60}, ctx)
        self.assertIn("All tracked devices reported", res)
