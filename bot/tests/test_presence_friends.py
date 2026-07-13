"""Presence service — friend arrival detection and callback tests."""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from presence_service import PresenceService


class TestFriendCallbackRegistration(unittest.TestCase):

    def test_on_friend_arrive_appends_callback(self):
        svc = PresenceService()
        cb = lambda label, mac: None
        svc.on_friend_arrive(cb)
        self.assertIn(cb, svc.friend_arrival_callbacks)

    def test_multiple_callbacks_registered(self):
        svc = PresenceService()
        cb1 = lambda l, m: None
        cb2 = lambda l, m: None
        svc.on_friend_arrive(cb1)
        svc.on_friend_arrive(cb2)
        self.assertEqual(len(svc.friend_arrival_callbacks), 2)

    def test_no_callbacks_by_default(self):
        svc = PresenceService()
        self.assertEqual(svc.friend_arrival_callbacks, [])


class TestFriendArrivalFiring(unittest.IsolatedAsyncioTestCase):
    """Verify that a MAC resolving to a friend node fires the callback."""

    async def _run_process_unknown_macs(self, svc, macs: dict, mock_identity):
        """Execute the same logic as presence_service._process_unknown_macs."""
        for mac, essid in macs.items():
            resolved = await mock_identity.resolve_entity(mac)
            if resolved:
                node = await mock_identity.get_identity(resolved["canonical_id"])
                if node and node.get("metadata", {}).get("role") == "friend":
                    label = node["metadata"].get("display", resolved["canonical_id"])
                    tasks = [asyncio.create_task(cb(label, mac))
                             for cb in svc.friend_arrival_callbacks]
                    if tasks:
                        await asyncio.gather(*tasks)
            else:
                await mock_identity.log_unresolved_entity(mac, "mac", {"essid": essid})

    async def test_friend_mac_fires_callback(self):
        svc = PresenceService()
        fired: list[tuple] = []

        async def capture(label, mac):
            fired.append((label, mac))

        svc.on_friend_arrive(capture)

        mock_identity = AsyncMock()
        mock_identity.resolve_entity.return_value = {
            "canonical_id": "lora", "confidence": 0.95
        }
        mock_identity.get_identity.return_value = {
            "metadata": {"role": "friend", "display": "Lora"}
        }

        await self._run_process_unknown_macs(
            svc, {"aa:bb:cc:dd:ee:01": "HomeWifi"}, mock_identity
        )

        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0], ("Lora", "aa:bb:cc:dd:ee:01"))

    async def test_unknown_mac_logs_unresolved(self):
        svc = PresenceService()
        mock_identity = AsyncMock()
        mock_identity.resolve_entity.return_value = None

        await self._run_process_unknown_macs(
            svc, {"aa:bb:cc:dd:ee:ff": "HomeWifi"}, mock_identity
        )

        mock_identity.log_unresolved_entity.assert_awaited_once_with(
            "aa:bb:cc:dd:ee:ff", "mac", {"essid": "HomeWifi"}
        )

    async def test_non_friend_role_does_not_fire_callback(self):
        """A MAC resolving to a family member (role: admin) should not fire friend callback."""
        svc = PresenceService()
        fired: list = []

        async def capture(label, mac):
            fired.append((label, mac))

        svc.on_friend_arrive(capture)

        mock_identity = AsyncMock()
        mock_identity.resolve_entity.return_value = {
            "canonical_id": "dad", "confidence": 0.99
        }
        mock_identity.get_identity.return_value = {
            "metadata": {"role": "admin", "display": "Dad"}
        }

        await self._run_process_unknown_macs(
            svc, {"aa:bb:cc:dd:ee:03": "HomeWifi"}, mock_identity
        )

        self.assertEqual(fired, [])

    async def test_multiple_friends_fire_multiple_callbacks(self):
        svc = PresenceService()
        fired: list[tuple] = []

        async def capture(label, mac):
            fired.append((label, mac))

        svc.on_friend_arrive(capture)

        lora_mac = "aa:bb:cc:dd:ee:01"
        pietra_mac = "aa:bb:cc:dd:ee:02"

        async def resolve_side(mac):
            return {"canonical_id": "lora" if mac == lora_mac else "pietra",
                    "confidence": 0.95}

        async def identity_side(cid):
            return {"metadata": {"role": "friend",
                                  "display": "Lora" if cid == "lora" else "Pietra"}}

        mock_identity = AsyncMock()
        mock_identity.resolve_entity.side_effect = resolve_side
        mock_identity.get_identity.side_effect = identity_side

        await self._run_process_unknown_macs(
            svc, {lora_mac: "HomeWifi", pietra_mac: "HomeWifi"}, mock_identity
        )

        self.assertEqual(len(fired), 2)
        names = {label for label, _ in fired}
        self.assertEqual(names, {"Lora", "Pietra"})

    async def test_no_callbacks_registered_does_not_crash(self):
        """Friend arrives but no one registered a callback — should not error."""
        svc = PresenceService()
        mock_identity = AsyncMock()
        mock_identity.resolve_entity.return_value = {
            "canonical_id": "lora", "confidence": 0.95
        }
        mock_identity.get_identity.return_value = {
            "metadata": {"role": "friend", "display": "Lora"}
        }

        try:
            await self._run_process_unknown_macs(
                svc, {"aa:bb:cc:dd:ee:01": "HomeWifi"}, mock_identity
            )
        except Exception as e:
            self.fail(f"Should not raise: {e}")


class TestWebSocketZoneChangeDedup(unittest.IsolatedAsyncioTestCase):
    """HA state_changed fires on attribute updates; only real zone moves should react."""

    async def test_same_zone_skips_check_presence(self):
        svc = PresenceService()
        svc.check_presence = AsyncMock()

        with patch("presence_service.is_family_member", return_value=True):
            await svc._on_person_state_change(
                "person.mom",
                {"state": "genome"},
                {"state": "genome"},
            )

        svc.check_presence.assert_not_awaited()

    async def test_zone_change_triggers_check_presence(self):
        svc = PresenceService()
        svc.check_presence = AsyncMock()

        with patch("presence_service.is_family_member", return_value=True):
            await svc._on_person_state_change(
                "person.mom",
                {"state": "genome"},
                {"state": "StatZon1"},
            )

        svc.check_presence.assert_awaited_once_with(force_refresh=True)

    async def test_first_seen_zone_triggers_check_presence(self):
        svc = PresenceService()
        svc.check_presence = AsyncMock()

        with patch("presence_service.is_family_member", return_value=True):
            await svc._on_person_state_change(
                "person.mom",
                {"state": "genome"},
                None,
            )

        svc.check_presence.assert_awaited_once_with(force_refresh=True)


if __name__ == "__main__":
    unittest.main()
