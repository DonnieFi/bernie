"""Test calendar cache TTL hit/miss/invalidate (no mock of unit under test)."""

import unittest
import asyncio
from datetime import datetime, timedelta, timezone

from calendar_service import CalendarService


class TestCalendarCache(unittest.TestCase):
    def setUp(self):
        config = {
            "timezone": "America/Halifax",
            "family_members": {"Dad": {"calendars": ["cal1"]}},
            "shared_calendars": [],
        }
        self.cal = CalendarService(config)
        self.cal._cache = {}
        self.cal._cache_ttl = 1.0  # small for test

    def test_cache_miss_then_hit(self):
        # mock the sync fetch to count calls (cache is in _fetch_events)
        calls = []
        def fake_sync(min, max):
            calls.append(1)
            return [{"id": "e1"}]
        self.cal._fetch_events_sync = fake_sync
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=1)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # first miss
            ev1 = loop.run_until_complete(self.cal._fetch_events(start, end))
            self.assertEqual(len(calls), 1)
            # second hit
            ev2 = loop.run_until_complete(self.cal._fetch_events(start, end))
            self.assertEqual(len(calls), 1)
            self.assertEqual(ev1, ev2)
        finally:
            loop.close()

    def test_invalidate(self):
        calls = []
        def fake_sync(min, max):
            calls.append(1)
            return [{"id": "e1"}]
        self.cal._fetch_events_sync = fake_sync
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.cal.get_todays_events())
            self.cal.invalidate_calendar_cache()
            loop.run_until_complete(self.cal.get_todays_events())
            self.assertEqual(len(calls), 2)
        finally:
            loop.close()

    def test_create_event_invalidates_cache_after_confirmed_write(self):
        class _FakeInsert:
            def execute(self):
                return {
                    "id": "created-1",
                    "summary": "Created",
                    "start": {"dateTime": "2026-06-01T14:30:00-03:00"},
                    "end": {"dateTime": "2026-06-01T15:00:00-03:00"},
                }

        class _FakeEvents:
            def insert(self, **kwargs):
                return _FakeInsert()

        class _FakeService:
            def events(self):
                return _FakeEvents()

        self.cal._cache[("a", "b")] = (0.0, [{"id": "stale"}])
        self.cal._get_service = lambda: _FakeService()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        start = datetime(2026, 6, 1, 14, 30, tzinfo=self.cal.tz)
        end = start + timedelta(minutes=30)

        try:
            event = loop.run_until_complete(self.cal.create_event("Created", start, end, []))
        finally:
            loop.close()

        self.assertEqual(event["id"], "created-1")
        self.assertEqual(self.cal._cache, {})


if __name__ == '__main__':
    unittest.main()
