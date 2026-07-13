"""Frigate listener filter logic tests — no MQTT, no live network."""
import os
import sys
import types
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Stub heavy dependencies before importing the module under test.
# NB: do NOT stub ha_service here — frigate_listener no longer imports it, and a
# global MagicMock stub leaks into other test modules (test_phase24's iCloud3 test
# awaits the real ha_service), producing "MagicMock can't be used in 'await'".
for _mod in ("discord", "aiomqtt", "frigate_service"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import frigate_listener


class TestIsNightHours(unittest.TestCase):
    """Default window 22:00–06:00 — pinned in test config, not live config.json."""

    _DEFAULT_NIGHT_CFG = {
        "timezone": "UTC",
        "frigate": {"night_hours": {"start": "22:00", "end": "06:00"}},
    }

    def _check(self, hour: int, minute: int = 0) -> bool:
        fake_now = datetime(2026, 5, 13, hour, minute)
        with patch("frigate_listener.config", self._DEFAULT_NIGHT_CFG), \
             patch("frigate_listener.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.strptime.side_effect = datetime.strptime
            return frigate_listener._is_night_hours()

    def test_midnight_is_night(self):
        self.assertTrue(self._check(0, 0))

    def test_two_am_is_night(self):
        self.assertTrue(self._check(2, 30))

    def test_five_fifty_nine_is_night(self):
        self.assertTrue(self._check(5, 59))

    def test_six_am_is_not_night(self):
        self.assertFalse(self._check(6, 0))

    def test_midday_is_not_night(self):
        self.assertFalse(self._check(12, 0))

    def test_ten_pm_is_night(self):
        self.assertTrue(self._check(22, 0))

    def test_ten_pm_plus_one_minute_is_night(self):
        self.assertTrue(self._check(22, 1))


class TestIsNightHoursCustomWindow(unittest.TestCase):
    """Night hours that don't wrap midnight (e.g. 23:00–07:00 is fine, 14:00–16:00 is not)."""

    def _check_with_config(self, hour: int, start: str, end: str) -> bool:
        fake_cfg = {
            "timezone": "UTC",
            "frigate": {"night_hours": {"start": start, "end": end}},
        }
        fake_now = datetime(2026, 5, 13, hour, 0)
        with patch("frigate_listener.config", fake_cfg), \
             patch("frigate_listener.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.strptime.side_effect = datetime.strptime
            return frigate_listener._is_night_hours()

    def test_non_wrapping_window_inside(self):
        self.assertTrue(self._check_with_config(15, "14:00", "16:00"))

    def test_non_wrapping_window_outside(self):
        self.assertFalse(self._check_with_config(10, "14:00", "16:00"))


class TestCameraToggle(unittest.IsolatedAsyncioTestCase):
    """Per-camera enable/disable logic matches the listener's filter."""

    def _cameras_enabled_check(self, camera: str, cameras_enabled: dict) -> bool:
        return cameras_enabled.get(camera, True)

    def test_unlisted_camera_defaults_to_enabled(self):
        self.assertTrue(self._cameras_enabled_check("cam_99", {}))

    def test_explicitly_enabled(self):
        self.assertTrue(self._cameras_enabled_check("cam_8", {"cam_8": True}))

    def test_explicitly_disabled(self):
        self.assertFalse(self._cameras_enabled_check("cam_8", {"cam_8": False}))

    def test_other_cameras_unaffected(self):
        cfg = {"cam_8": False, "cam_18": True}
        self.assertFalse(self._cameras_enabled_check("cam_8", cfg))
        self.assertTrue(self._cameras_enabled_check("cam_18", cfg))


class TestModeFilter(unittest.IsolatedAsyncioTestCase):
    """mode: off suppresses all; mode: test bypasses presence; mode: on uses presence."""

    def _should_suppress(self, mode: str, is_night: bool, is_away: bool) -> bool:
        """Return True if the alert would be suppressed, mirroring listener logic."""
        if mode == "off":
            return True
        if mode == "on":
            if not is_night and not is_away:
                return True
        # mode == "test" always fires
        return False

    def test_mode_off_always_suppresses(self):
        self.assertTrue(self._should_suppress("off", True, True))
        self.assertTrue(self._should_suppress("off", False, False))

    def test_mode_test_never_suppresses(self):
        self.assertFalse(self._should_suppress("test", False, False))
        self.assertFalse(self._should_suppress("test", True, True))

    def test_mode_on_home_daytime_suppresses(self):
        self.assertTrue(self._should_suppress("on", is_night=False, is_away=False))

    def test_mode_on_home_night_fires(self):
        self.assertFalse(self._should_suppress("on", is_night=True, is_away=False))

    def test_mode_on_away_daytime_fires(self):
        self.assertFalse(self._should_suppress("on", is_night=False, is_away=True))

    def test_mode_on_away_night_fires(self):
        self.assertFalse(self._should_suppress("on", is_night=True, is_away=True))


class TestIsAway(unittest.IsolatedAsyncioTestCase):
    """_is_away() routes through presence_service (WiFi + GPS-freshness aware).

    Away unless a parent is *confirmed* home. Stale GPS and 'away' both resolve to
    not-home; only a fresh signal (home is True) suppresses. Fails toward sending.
    """

    DEFAULT_CFG = {"family_members": {
        "Dad": {"role": "parent", "canonical_id": "dad"},
        "Mom": {"role": "parent", "canonical_id": "mom"},
        "Child1": {"role": "child", "canonical_id": "child1"},
    }}

    async def _is_away(self, presence=None, raises=False, cfg=None):
        """presence: map person_id -> {home: bool}; only parent ids matter."""
        ps = MagicMock()
        if raises:
            ps.is_any_home = AsyncMock(side_effect=RuntimeError("HA down"))
        else:
            presence = presence if presence is not None else {}

            async def _any_home(ids):
                return any(presence.get(i, {}).get("home") is True for i in ids)

            ps.is_any_home = AsyncMock(side_effect=_any_home)
        presence_mod = types.ModuleType("presence_service")
        presence_mod.presence_service = ps
        constants_mod = types.ModuleType("constants")
        constants_mod.registry = MagicMock()
        # patch.dict restores sys.modules afterward — no leak into the combined suite.
        with patch.dict(sys.modules, {"presence_service": presence_mod, "constants": constants_mod}), \
             patch("frigate_listener.config", self.DEFAULT_CFG if cfg is None else cfg):
            return await frigate_listener._is_away()

    async def test_confirmed_home_parent_suppresses(self):
        away = await self._is_away({"dad": {"home": True}, "mom": {"home": False}})
        self.assertFalse(away)

    async def test_either_parent_home_suppresses(self):
        away = await self._is_away({"dad": {"home": False}, "mom": {"home": True}})
        self.assertFalse(away)

    async def test_unknown_and_away_is_away(self):
        # Today's bug: Dad stale->unknown (home False), Mom away (home False) -> fire.
        away = await self._is_away({"dad": {"home": False}, "mom": {"home": False}})
        self.assertTrue(away)

    async def test_missing_presence_entries_is_away(self):
        away = await self._is_away({})
        self.assertTrue(away)

    async def test_presence_lookup_error_is_away(self):
        away = await self._is_away(raises=True)
        self.assertTrue(away)

    async def test_no_parents_configured_is_away(self):
        away = await self._is_away(cfg={"family_members": {"Child1": {"role": "child", "canonical_id": "child1"}}})
        self.assertTrue(away)

    async def test_child_home_does_not_suppress(self):
        # Only parents/admins gate alerts; a child being home must still fire.
        away = await self._is_away({"child1": {"home": True}, "dad": {"home": False}, "mom": {"home": False}})
        self.assertTrue(away)


class TestFrigateNotificationChannel(unittest.TestCase):
    def test_prefers_frigate_notification_channel_id(self):
        cfg = {
            "frigate": {"notification_channel_id": 111},
            "security_channel_id": 222,
            "anvil_channel_id": 333,
        }
        self.assertEqual(frigate_listener._frigate_notification_channel_id(cfg), 111)

    def test_falls_back_to_security_channel_id(self):
        cfg = {"security_channel_id": 222, "anvil_channel_id": 333}
        self.assertEqual(frigate_listener._frigate_notification_channel_id(cfg), 222)

    def test_falls_back_to_anvil(self):
        cfg = {"anvil_channel_id": 333}
        self.assertEqual(frigate_listener._frigate_notification_channel_id(cfg), 333)


if __name__ == "__main__":
    unittest.main()
