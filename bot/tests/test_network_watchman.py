"""Tests for network watchman — IP tracking, infra events, timeline."""

import inspect
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import database as db


async def _local_db_routed(op: str, /, *args, **kwargs):
    """Route db_writes.routed to in-process database (no cognition RPC in unittest)."""
    handler = getattr(db, op, None)
    if handler is None:
        raise ValueError(f"unknown write op: {op!r}")
    if args:
        params = list(inspect.signature(handler).parameters.keys())
        bound = dict(zip(params, args))
        bound.update(kwargs)
        kwargs = bound
    return await handler(**kwargs)


class NetworkWatchmanHelpersTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from db_binding import bind_database
        bind_database(db)
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        db.DB_PATH = self._tmp.name
        await db.ensure_network_watchman_schema()
        self._routed_patch = patch(
            "network_watchman.db_writes.routed", side_effect=_local_db_routed,
        )
        self._routed_patch.start()

    async def asyncTearDown(self):
        self._routed_patch.stop()
        await db.close_db()
        os.unlink(self._tmp.name)

    def test_allowed_ips_merges_alt(self):
        from network_watchman import _allowed_ips
        cfg = {"ips": ["192.168.1.X"], "alt_ips": ["192.168.86.41"]}
        self.assertEqual(
            _allowed_ips(cfg),
            {"192.168.1.X", "192.168.86.41"},
        )

    def test_classify_ip_change_deba_allowed(self):
        from network_watchman import _classify_ip_change, EVENT_IP_CHANGE
        cfg = {"ips": ["192.168.1.X", "192.168.1.X"]}
        ev = _classify_ip_change("deba", cfg, "192.168.1.X", "192.168.1.X")
        self.assertEqual(ev["event_type"], EVENT_IP_CHANGE)
        self.assertEqual(ev["severity"], "info")

    def test_classify_ip_change_unexpected(self):
        from network_watchman import _classify_ip_change, EVENT_IP_UNEXPECTED
        cfg = {"ips": ["192.168.1.X"], "services": ["pihole"]}
        ev = _classify_ip_change("aka", cfg, "192.168.1.X", "192.168.1.X")
        self.assertEqual(ev["event_type"], EVENT_IP_UNEXPECTED)
        self.assertEqual(ev["severity"], "critical")

    def test_classify_ip_change_unexpected_no_services(self):
        from network_watchman import _classify_ip_change, EVENT_IP_UNEXPECTED
        cfg = {"ips": ["192.168.1.X"]}
        ev = _classify_ip_change("yanagiba", cfg, "192.168.1.X", "192.168.1.X")
        self.assertEqual(ev["event_type"], EVENT_IP_UNEXPECTED)
        self.assertEqual(ev["severity"], "warn")

    def test_parse_caddy_ips(self):
        from network_watchman import _parse_caddy_ips
        with tempfile.NamedTemporaryFile("w", suffix=".Caddyfile", delete=False) as f:
            f.write("ha.lan {\n  reverse_proxy 192.168.1.X:8123\n}\n")
            path = f.name
        try:
            m = _parse_caddy_ips(path)
            self.assertIn("ha.lan", m)
            self.assertIn("192.168.1.X", m["ha.lan"])
        finally:
            os.unlink(path)

    async def test_record_and_list_events(self):
        await db.record_network_event("ip_change", "aka IP changed", host_id="aka", severity="warn")
        events = await db.list_network_events(limit=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["host_id"], "aka")

    def test_host_match_rejects_ha_substring(self):
        from network_watchman import _host_matches
        cfg = {"ips": ["192.168.1.X"], "match_names": ["homeassistant"]}
        device = {"display_name": "Harmony Hub", "hostname": "harmony", "ip": "192.168.86.32"}
        self.assertFalse(_host_matches(device, "ha", cfg))

    def test_name_matches_rejects_short_device_in_long_config(self):
        from network_watchman import _name_matches
        self.assertFalse(_name_matches("home", "homeassistant"))
        self.assertFalse(_name_matches("assistant", "homeassistant"))
        self.assertTrue(_name_matches("homeassistant", "homeassistant"))
        self.assertTrue(_name_matches("homeassistant.local", "homeassistant"))

    def test_parse_caddy_ips_block_style(self):
        from network_watchman import _parse_caddy_ips
        content = (
            "# legacy inline proxy\n"
            "ha.lan {\n"
            "    reverse_proxy {\n"
            "        to 192.168.1.X:8123\n"
            "    }\n"
            "}\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".Caddyfile", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            m = _parse_caddy_ips(path)
            self.assertIn("ha.lan", m)
            self.assertIn("192.168.1.X", m["ha.lan"])
        finally:
            os.unlink(path)

    async def test_wifi_drop_skipped_when_sta_unavailable(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from network_watchman import EVENT_WIFI_CLIENT_DROP, poll, _META_WIFI

        await db.upsert_host_ip_snapshot(_META_WIFI, "25", True, "", is_online=True)

        def _fake_network(infra):
            return SimpleNamespace(
                get_devices=AsyncMock(return_value=[]),
                fetch_unifi_snapshot=AsyncMock(return_value={}),
                infra_from_unifi_snapshot=lambda snap: infra,
            )

        fake_network = _fake_network({
            "offline_aps": [],
            "wifi_clients": None,
            "wired_clients": None,
            "sta_available": False,
        })
        with (
            patch("network_watchman._enabled", return_value=True),
            patch("network_watchman._critical_hosts", return_value={}),
            patch("network_watchman._cfg", return_value={}),
            patch.dict(sys.modules, {"network_service": SimpleNamespace(network_service=fake_network)}),
        ):
            events = await poll()
        drop_events = [e for e in events if e.get("event_type") == EVENT_WIFI_CLIENT_DROP]
        self.assertEqual(drop_events, [])
        snap = await db.get_host_ip_snapshot(_META_WIFI)
        self.assertEqual(snap["ip"], "25")

    async def test_caddy_check_skipped_when_not_due(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from network_watchman import EVENT_CADDY_STALE, poll, _META_CADDY

        await db.upsert_host_ip_snapshot(_META_CADDY, "ok", True, "", is_online=True)

        fake_network = SimpleNamespace(
            get_devices=AsyncMock(return_value=[]),
            fetch_unifi_snapshot=AsyncMock(return_value={}),
            infra_from_unifi_snapshot=lambda snap: {
                "offline_aps": [], "wifi_clients": 0, "wired_clients": 0, "sta_available": True,
            },
        )
        with (
            patch("network_watchman._enabled", return_value=True),
            patch("network_watchman._critical_hosts", return_value={}),
            patch("network_watchman._cfg", return_value={"caddyfile_path": "/tmp/nope"}),
            patch("network_watchman._parse_caddy_ips") as mock_parse,
            patch.dict(sys.modules, {"network_service": SimpleNamespace(network_service=fake_network)}),
        ):
            events = await poll()
        mock_parse.assert_not_called()
        self.assertEqual([e for e in events if e.get("event_type") == EVENT_CADDY_STALE], [])

    async def test_caddy_check_runs_when_due(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from network_watchman import EVENT_CADDY_STALE, poll

        fake_network = SimpleNamespace(
            get_devices=AsyncMock(return_value=[]),
            fetch_unifi_snapshot=AsyncMock(return_value={}),
            infra_from_unifi_snapshot=lambda snap: {
                "offline_aps": [], "wifi_clients": 0, "wired_clients": 0, "sta_available": True,
            },
        )
        with (
            patch("network_watchman._enabled", return_value=True),
            patch("network_watchman._critical_hosts", return_value={}),
            patch("network_watchman._cfg", return_value={"caddyfile_path": "/tmp/caddy"}),
            patch("network_watchman._parse_caddy_ips", return_value={"grafana.lan": {"192.168.1.X"}}) as mock_parse,
            patch.dict(sys.modules, {"network_service": SimpleNamespace(network_service=fake_network)}),
        ):
            events = await poll()
        mock_parse.assert_called_once()
        self.assertEqual(len([e for e in events if e.get("event_type") == EVENT_CADDY_STALE]), 1)

    def test_format_overnight_timeline_empty(self):
        from network_watchman import format_overnight_timeline
        self.assertIn("no events", format_overnight_timeline([]))

    def test_format_host_snapshot_unexpected_ip(self):
        from network_watchman import format_host_snapshot
        snap = {"ip": "192.168.1.X", "is_wired": 1, "is_online": 1, "essid": ""}
        cfg = {"ips": ["192.168.1.X"]}
        line = format_host_snapshot("aka", snap, cfg)
        self.assertIn("unexpected", line)
        self.assertIn("192.168.1.X", line)


if __name__ == "__main__":
    unittest.main()
