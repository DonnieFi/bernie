"""Unit tests for transit_service (no live feed)."""
from __future__ import annotations

import unittest

from transit_service import (
    VehicleSnapshot,
    ZoneLandmark,
    clear_zone_cache_for_tests,
    filter_route,
    format_proximity,
    format_route_list,
    haversine_m,
    list_landmark_choices,
    nearest_vehicle,
    normalize_route_id,
    LatLon,
    _parse_feed_bytes,
    _speed_kmh,
)


class TestTransitMath(unittest.TestCase):
    def test_normalize_route_id(self):
        self.assertEqual(normalize_route_id("04"), "4")
        self.assertEqual(normalize_route_id("4"), "4")
        self.assertEqual(normalize_route_id("1A"), "1A")

    def test_haversine_positive(self):
        d = haversine_m(44.65, -63.59, 44.64, -63.58)
        self.assertGreater(d, 100)
        self.assertLess(d, 5000)

    def test_speed_ms_to_kmh(self):
        self.assertAlmostEqual(_speed_kmh(10.0), 36.0)

    def test_filter_route(self):
        vehicles = [
            VehicleSnapshot("1", "4", 44.0, -63.0, None, None, None),
            VehicleSnapshot("2", "1", 44.1, -63.1, None, None, None),
        ]
        self.assertEqual(len(filter_route(vehicles, "4")), 1)

    def test_nearest_vehicle(self):
        vehicles = [
            VehicleSnapshot("far", "4", 44.70, -63.70, None, None, None),
            VehicleSnapshot("near", "4", 44.641, -63.582, None, 5.0, None),
        ]
        target = LatLon(44.641, -63.582, "Sacred Heart", 84.0)
        bus, dist = nearest_vehicle(vehicles, target)
        self.assertEqual(bus.vehicle_id, "near")
        self.assertLess(dist, 200)

    def test_format_route_list_empty(self):
        self.assertIn("No active", format_route_list([], "4"))

    def test_format_proximity_contains_distance(self):
        v = VehicleSnapshot("3160", "4", 44.641, -63.582, 56.0, 12.0, None)
        t = LatLon(44.653, -63.595, "Home", 30.0)
        text = format_proximity(v, 420.0, t)
        self.assertIn("3160", text)
        self.assertIn("420", text)
        self.assertIn("maps.google.com", text)


class TestTransitZones(unittest.TestCase):
    def tearDown(self):
        clear_zone_cache_for_tests()

    def test_list_landmark_choices_includes_caller(self):
        from transit_service import _ZONES, _ZONES_FETCHED_AT
        import transit_service as ts

        ts._ZONES = {
            "home": ZoneLandmark("zone.home", "home", "Home", 44.65, -63.59, 30.0),
        }
        choices = list_landmark_choices()
        self.assertIn("caller", choices)


class TestTransitParse(unittest.TestCase):
    def test_parse_empty_feed(self):
        try:
            from google.transit import gtfs_realtime_pb2
        except ImportError:
            self.skipTest("gtfs-realtime-bindings not installed")

        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.timestamp = 1700000000
        data = feed.SerializeToString()
        self.assertEqual(_parse_feed_bytes(data), [])


if __name__ == "__main__":
    unittest.main()
