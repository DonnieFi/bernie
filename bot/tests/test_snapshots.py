"""Unit tests for snapshot profiles and tools."""
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from executor import ServiceRefs, ToolContext
from snapshot_profiles import (
    VEHICLE_PROFILES,
    fetch_vehicle_status,
    format_duration_minutes,
)
from tools import snapshots as snapshots_tools


class TestFormatDuration(unittest.TestCase):
    def test_hours_and_minutes(self):
        self.assertEqual(format_duration_minutes(390), "6h 30m")

    def test_minutes_only(self):
        self.assertEqual(format_duration_minutes(45), "45m")

    def test_hours_only(self):
        self.assertEqual(format_duration_minutes(120), "2h")


class TestVehicleLockEntity(unittest.IsolatedAsyncioTestCase):
    async def test_lock_uses_lock_entity_not_binary_sensor(self):
        """lock unlocked + binary_sensor on must still report unlocked from lock entity."""
        ha = MagicMock()
        states = {
            "lock.nirochan_door_lock": {"entity_id": "lock.nirochan_door_lock", "state": "unlocked"},
            "binary_sensor.nirochan_locked": {"entity_id": "binary_sensor.nirochan_locked", "state": "on"},
            "sensor.nirochan_ev_battery_level": {
                "entity_id": "sensor.nirochan_ev_battery_level",
                "state": "82",
            },
        }

        async def get_state(entity_id: str):
            return states.get(entity_id, {})

        ha.get_state = AsyncMock(side_effect=get_state)

        status = await fetch_vehicle_status(ha, "nirochan", extras=False)
        self.assertIsNotNone(status)
        self.assertEqual(status.core.lock, "unlocked")

        called = {call.args[0] for call in ha.get_state.await_args_list}
        self.assertIn("lock.nirochan_door_lock", called)
        self.assertNotIn("binary_sensor.nirochan_locked", called)
        self.assertNotIn("binary_sensor.nirochan_locked", VEHICLE_PROFILES["nirochan"]["core"].values())
        self.assertNotIn("binary_sensor.nirochan_locked", VEHICLE_PROFILES["nirochan"]["extras"].values())


class TestVehicleStatusTool(unittest.IsolatedAsyncioTestCase):
    async def test_get_vehicle_status_returns_json_with_summary(self):
        ha = MagicMock()

        async def get_state(entity_id: str):
            mapping = {
                "lock.nirochan_door_lock": {"entity_id": entity_id, "state": "locked"},
                "sensor.nirochan_ev_battery_level": {"entity_id": entity_id, "state": "90"},
                "binary_sensor.nirochan_ev_battery_plug": {"entity_id": entity_id, "state": "off"},
                "switch.nirochan_ev_charging": {"entity_id": entity_id, "state": "off"},
                "device_tracker.nirochan_location": {"entity_id": entity_id, "state": "home"},
                "sensor.nirochan_last_updated_at": {"entity_id": entity_id, "state": "2026-05-28T08:00:00"},
            }
            return mapping.get(entity_id, {})

        ha.get_state = AsyncMock(side_effect=get_state)
        ctx = ToolContext(
            config={},
            person_id="dad",
            group="family",
            channel_id=None,
            shadow=False,
            executor="native",
            services=ServiceRefs(ha=ha),
        )

        raw = await snapshots_tools.handle_get_vehicle_status({"vehicle": "nirochan", "extras": False}, ctx)
        payload = json.loads(raw)
        self.assertIn("summary", payload)
        self.assertIn("core", payload)
        self.assertEqual(payload["core"]["lock"], "locked")
        self.assertEqual(payload["core"]["ev_battery_pct"], 90.0)
        self.assertIn("locked", payload["summary"].lower())
