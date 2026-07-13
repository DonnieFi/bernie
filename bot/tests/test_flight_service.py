"""Unit tests for flight_service (mocked AeroAPI)."""
from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from flight_service import (
    FlightPhase,
    clear_flight_cache_for_tests,
    flight_status_to_json,
    track_flight,
    _derive_phase,
    _pick_flight,
    _normalize_ident,
)


SAMPLE_LEG = {
    "ident": "OCN74",
    "ident_icao": "OCN74",
    "ident_iata": "4Y74",
    "fa_flight_id": "OCN74-1752048000-airline-0123",
    "status": "En Route / On Time",
    "cancelled": False,
    "diverted": False,
    "progress_percent": 62,
    "origin": {"code_iata": "FRA", "timezone": "Europe/Berlin", "name": "Frankfurt"},
    "destination": {"code_iata": "YHZ", "timezone": "America/Halifax", "name": "Halifax"},
    "scheduled_out": "2026-07-09T10:00:00Z",
    "estimated_out": "2026-07-09T10:05:00Z",
    "actual_out": "2026-07-09T10:12:00Z",
    "scheduled_in": "2026-07-09T18:30:00Z",
    "estimated_in": "2026-07-09T18:22:00Z",
    "actual_off": "2026-07-09T10:25:00Z",
}

SAMPLE_POSITION = {
    "last_position": {
        "latitude": 50.2,
        "longitude": -40.5,
        "altitude": 350,
        "groundspeed": 480,
        "heading": 270,
        "timestamp": "2026-07-09T14:00:00Z",
    }
}

LANDED_LEG = {
    **SAMPLE_LEG,
    "status": "Arrived / Gate Arrival",
    "actual_on": "2026-07-09T18:20:00Z",
    "actual_in": "2026-07-09T18:35:00Z",
}


class TestFlightHelpers(unittest.TestCase):
    def test_normalize_ident(self):
        self.assertEqual(_normalize_ident("  ocn74 "), "OCN74")

    def test_derive_phase_en_route(self):
        self.assertEqual(_derive_phase(SAMPLE_LEG), FlightPhase.en_route)

    def test_derive_phase_landed(self):
        self.assertEqual(_derive_phase(LANDED_LEG), FlightPhase.landed)

    def test_pick_flight_prefers_en_route(self):
        scheduled = {**SAMPLE_LEG, "actual_off": None, "actual_out": None, "status": "Scheduled"}
        picked = _pick_flight([scheduled, SAMPLE_LEG])
        self.assertEqual(picked["status"], "En Route / On Time")

    def test_parse_dt_naive_becomes_utc(self):
        from flight_service import _parse_dt
        from datetime import timezone

        dt = _parse_dt("2026-07-09T12:00:00")
        self.assertIsNotNone(dt.tzinfo)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_parse_dt_z_suffix(self):
        from flight_service import _parse_dt

        dt = _parse_dt("2026-07-09T12:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 12)


class TestTrackFlight(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_flight_cache_for_tests()

    def tearDown(self):
        clear_flight_cache_for_tests()

    async def test_en_route_fetches_position(self):
        with patch.dict("os.environ", {"FLIGHT_AERO_KEY": "test-key"}), patch(
            "flight_service._fetch_flight_leg",
            new=AsyncMock(return_value=SAMPLE_LEG),
        ), patch(
            "flight_service._fetch_position",
            new=AsyncMock(return_value=SAMPLE_POSITION),
        ) as pos_mock:
            result = await track_flight("4Y74")
        pos_mock.assert_awaited_once()
        self.assertEqual(result.phase, FlightPhase.en_route)
        self.assertIsNotNone(result.position)
        self.assertAlmostEqual(result.position.latitude, 50.2)
        self.assertEqual(result.position.altitude_ft, 35000)
        self.assertIn("North Atlantic", result.relative_position or "")
        self.assertIn("maps.google.com", result.google_maps_url or "")
        self.assertIn("staticmap.openstreetmap.de", result.static_map_image_url or "")
        self.assertIn("flightaware.com", result.map_url or "")

    async def test_landed_skips_position_api(self):
        with patch.dict("os.environ", {"FLIGHT_AERO_KEY": "test-key"}), patch(
            "flight_service._fetch_flight_leg",
            new=AsyncMock(return_value=LANDED_LEG),
        ), patch(
            "flight_service._fetch_position",
            new=AsyncMock(),
        ) as pos_mock:
            result = await track_flight("OCN74")
        pos_mock.assert_not_called()
        self.assertEqual(result.phase, FlightPhase.landed)
        self.assertIn("Landed", result.summary)

    async def test_landed_shows_destination_map(self):
        with patch.dict("os.environ", {"FLIGHT_AERO_KEY": "test-key"}), patch(
            "flight_service._fetch_flight_leg",
            new=AsyncMock(return_value=LANDED_LEG),
        ):
            result = await track_flight("OCN74")
        self.assertEqual(result.phase, FlightPhase.landed)
        self.assertIsNotNone(result.google_maps_url)
        self.assertIn("maps.google.com", result.google_maps_url)
        self.assertIn("44.88", result.google_maps_url)  # YHZ approx

    async def test_json_payload_shape(self):
        with patch.dict("os.environ", {"FLIGHT_AERO_KEY": "test-key"}), patch(
            "flight_service._fetch_flight_leg",
            new=AsyncMock(return_value=LANDED_LEG),
        ):
            result = await track_flight("OCN74")
        payload = json.loads(flight_status_to_json(result))
        self.assertIn("summary", payload)
        self.assertIn("core", payload)
        self.assertEqual(payload["core"]["phase"], "landed")


if __name__ == "__main__":
    unittest.main()
