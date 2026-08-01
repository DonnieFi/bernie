import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from presence.adapters import HANetworkPresenceAdapter, UniFiPresenceAdapter


class TestPresenceAdapters(unittest.IsolatedAsyncioTestCase):
    async def test_unifi_empty_without_key(self):
        adapter = UniFiPresenceAdapter("https://192.168.1.X", None)
        self.assertEqual(await adapter.fetch_active_clients(), {})

    async def test_ha_adapter_returns_mac_map(self):
        adapter = HANetworkPresenceAdapter(
            {"home_assistant": {"network_scanner_entity": "sensor.scanner"}}
        )
        mock_state = {
            "attributes": {
                "devices": [{"mac": "AA:BB:CC:DD:EE:FF"}],
                "friendly_name": "Test WiFi",
            }
        }
        with patch("ha_service.ha_service") as mock_ha:
            mock_ha.get_state = AsyncMock(return_value=mock_state)
            result = await adapter.fetch_active_clients()
        self.assertIn("aa:bb:cc:dd:ee:ff", result)
        self.assertEqual(result["aa:bb:cc:dd:ee:ff"]["essid"], "Test WiFi")

    async def test_presence_service_uses_adapters(self):
        from presence_service import PresenceService

        mock_adapter1 = AsyncMock()
        mock_adapter1.fetch_active_clients.return_value = {
            "aa:bb:cc:dd:ee:ff": {"essid": "WiFi1", "ip": "192.168.1.X"}
        }
        mock_adapter2 = AsyncMock()
        mock_adapter2.fetch_active_clients.return_value = {
            "22:33:44:55:66:77": {"essid": "WiFi2", "ip": None}
        }

        svc = PresenceService(adapters=[mock_adapter1, mock_adapter2])

        cfg = {
            "family_members": {}
        }
        mock_db = MagicMock()
        mock_db.set_last_home_signal = AsyncMock()
        mock_db.get_last_home_signal = AsyncMock(return_value=0.0)  # epoch = always stale
        mock_db.update_presence = AsyncMock(return_value=False)
        with patch("config.config", cfg), \
             patch("presence_service.get_database", return_value=mock_db):
            await svc.check_presence()

        mock_adapter1.fetch_active_clients.assert_awaited_once()
        mock_adapter2.fetch_active_clients.assert_awaited_once()

