"""family-bot-04bf.2: read-only calendar range API."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from api.routes.calendar import _range, build_calendar_router, SchoolScheduleToggle


TZ = ZoneInfo("America/Halifax")


def _endpoint(router, path):
    return next(route.endpoint for route in router.routes if route.path == path)


class TestCalendarRangeApi(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.day = datetime(2026, 7, 22, tzinfo=TZ)
        self.events = [
            {
                "id": "soccer", "summary": "Soccer", "start": self.day,
                "end": self.day + timedelta(hours=1), "due_date": self.day,
                "all_day": False, "calendar_id": "family",
                "attendees": ["Child1"], "location": "Field",
            },
            {
                "id": "math", "summary": "Math", "start": self.day,
                "end": self.day + timedelta(hours=1), "due_date": self.day,
                "all_day": False, "calendar_id": "school",
                "attendees": [], "location": "",
            },
            {
                "id": "uniform", "summary": "PE uniform", "start": self.day,
                "end": self.day + timedelta(days=1), "due_date": self.day,
                "all_day": True, "calendar_id": "school",
                "attendees": [], "location": "",
            },
        ]
        self.calendar = AsyncMock()
        self.calendar.get_events_between.return_value = self.events
        self.db = AsyncMock()
        self.router = build_calendar_router(SimpleNamespace(calendar_service=self.calendar, db=self.db))

    async def test_range_projects_grid_and_uniform(self):
        with patch("api.routes.calendar._ac.config", {
            "school_calendars": [{"id": "school", "student": "Child2"}],
        }):
            data = await _endpoint(self.router, "/api/calendar")(
                start="2026-07-22", end="2026-07-22"
            )
        self.assertEqual([event["title"] for event in data["events"]], ["Soccer"])
        self.assertEqual(data["events"][0]["person_ids"], ["Child1"])
        self.assertEqual(data["uniform_by_date"]["2026-07-22"][0]["person_id"], "Child2")
        self.assertTrue(data["show_school"])
        self.calendar.get_events_between.assert_awaited_once_with("2026-07-22", "2026-07-22")

    async def test_school_off_hides_uniform_and_homework_layers(self):
        self.events.append({
            "id": "essay", "summary": "History essay", "start": self.day,
            "end": self.day + timedelta(days=1), "due_date": self.day,
            "all_day": True, "calendar_id": "school",
            "attendees": [], "location": "",
        })
        with patch("api.routes.calendar._ac.config", {
            "school_calendars": [{"id": "school", "student": "Child2"}],
            "show_school_in_daily_summary": False,
        }):
            data = await _endpoint(self.router, "/api/calendar")(
                start="2026-07-22", end="2026-07-22"
            )
        self.assertFalse(data["show_school"])
        self.assertEqual(data["uniform_by_date"], {})
        self.assertEqual(data["homework_by_date"], {})

    def test_rejects_reverse_range(self):
        with self.assertRaises(HTTPException):
            _range("2026-07-23", "2026-07-22")


class TestCalendarSchoolToggle(unittest.IsolatedAsyncioTestCase):
    async def test_patch_requires_parent(self):
        router = build_calendar_router(SimpleNamespace(calendar_service=AsyncMock(), db=AsyncMock()))
        kid = SimpleNamespace(id="person:child1", role="kids")
        with self.assertRaises(HTTPException) as ctx:
            await _endpoint(router, "/api/calendar/school-schedule")(
                SchoolScheduleToggle(enabled=False),
                user=kid,
            )
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_patch_updates_config(self):
        router = build_calendar_router(SimpleNamespace(calendar_service=AsyncMock(), db=AsyncMock()))
        parent = SimpleNamespace(id="person:red", role="parent")
        with patch("config.update_config", new_callable=AsyncMock) as update:
            out = await _endpoint(router, "/api/calendar/school-schedule")(
                SchoolScheduleToggle(enabled=False),
                user=parent,
            )
        update.assert_awaited_once_with({"show_school_in_daily_summary": False})
        self.assertFalse(out["show_school"])


if __name__ == "__main__":
    unittest.main()
