"""xcq.6 — config-driven snapshot profiles."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import snapshot_profiles as sp


class TestSnapshotProfilesConfig(unittest.TestCase):
    def test_builtin_fallback_when_snapshot_block_missing(self):
        """Private deploy: no snapshot_profiles key → builtins."""
        with patch.object(sp, "_config_snapshot_block", return_value={}):
            self.assertIn("nirochan", sp.vehicle_profiles())

    def test_empty_maps_are_intentional_oss(self):
        """Present empty vehicles/sleep maps must not leak family builtins."""
        with patch.object(sp, "_config_snapshot_block", return_value={"vehicles": {}, "sleep": {}}):
            self.assertEqual(sp.vehicle_profiles(), {})
            self.assertEqual(sp.sleep_profiles(), {})

    def test_config_vehicles_override(self):
        custom = {
            "vehicles": {
                "mycar": {
                    "core": {"lock": "lock.x", "ev_battery_pct": "sensor.x"},
                    "extras": {},
                }
            }
        }
        with patch.object(sp, "_config_snapshot_block", return_value=custom):
            profiles = sp.vehicle_profiles()
            self.assertIn("mycar", profiles)
            self.assertNotIn("nirochan", profiles)
            self.assertEqual(profiles["mycar"]["core"]["lock"], "lock.x")


if __name__ == "__main__":
    unittest.main()
