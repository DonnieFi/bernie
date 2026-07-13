"""family-bot-1bf.3: UniFi snapshot shared by devices + watchman infra."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from network_service import NetworkService


class TestUnifiSnapshot(unittest.IsolatedAsyncioTestCase):
    def test_infra_from_snapshot_counts(self):
        snap = {
            "device": [
                {"type": "uap", "name": "AP-Living", "state": 0, "model": "U6"},
                {"type": "usw", "name": "Switch", "state": 1, "model": "USW"},
            ],
            "sta": [
                {"is_wired": False, "mac": "aa:bb:cc:dd:ee:01"},
                {"is_wired": True, "mac": "aa:bb:cc:dd:ee:02"},
                {"is_wired": False, "mac": "aa:bb:cc:dd:ee:03"},
            ],
            "sta_available": True,
        }
        infra = NetworkService.infra_from_unifi_snapshot(snap)
        self.assertEqual(infra["offline_aps"], ["AP-Living"])
        self.assertEqual(infra["wifi_clients"], 2)
        self.assertEqual(infra["wired_clients"], 1)
        self.assertTrue(infra["sta_available"])

    def test_merge_unifi_into_devices(self):
        svc = NetworkService()
        devices: dict = {}
        snap = {
            "alluser": [
                {
                    "mac": "AA-BB-CC-DD-EE-FF",
                    "name": "Niro",
                    "hostname": "niro",
                    "ip": "192.168.1.X",
                    "is_wired": False,
                }
            ],
            "sta": [
                {
                    "mac": "aa:bb:cc:dd:ee:ff",
                    "ip": "192.168.1.X",
                    "name": "Niro",
                    "rx_bytes-r": 1_000_000,
                    "tx_bytes-r": 500_000,
                }
            ],
        }
        svc._merge_unifi_into_devices(devices, snap)
        d = devices["aa:bb:cc:dd:ee:ff"]
        self.assertTrue(d["is_active"])
        self.assertEqual(d["unifi_name"], "Niro")
        self.assertEqual(d["ip"], "192.168.1.X")

    async def test_get_devices_reuses_snapshot_no_extra_fetch(self):
        svc = NetworkService()
        svc._load_stored = AsyncMock(return_value={})
        svc.fetch_unifi_snapshot = AsyncMock(
            side_effect=AssertionError("must not re-fetch when snapshot passed")
        )
        snap = {
            "alluser": [],
            "sta": [{"mac": "11:22:33:44:55:66", "ip": "10.0.0.2", "name": "pi"}],
            "device": [],
            "sta_available": True,
        }
        with patch("network_service.ha_service.get_state", new_callable=AsyncMock, return_value={}):
            with patch("network_service.config", {"home_assistant": {}, "presence": {}}):
                devices = await svc.get_devices(unifi_snapshot=snap)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["mac"], "11:22:33:44:55:66")
        self.assertTrue(devices[0]["is_active"])


if __name__ == "__main__":
    unittest.main()
