"""Tests for school calendar daily-summary toggle."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from school_calendar import (
    exclude_school_from_schedule,
    school_calendar_ids,
    show_school_in_daily_summary,
)


class TestSchoolCalendarToggle(unittest.TestCase):
    def test_ids_from_strings(self):
        cfg = {"school_calendars": ["cal-a", "cal-b"]}
        self.assertEqual(school_calendar_ids(cfg), {"cal-a", "cal-b"})

    def test_ids_from_objects(self):
        cfg = {"school_calendars": [{"id": "cal-a"}, {"calendar_id": "cal-b"}]}
        self.assertEqual(school_calendar_ids(cfg), {"cal-a", "cal-b"})

    def test_default_show_school_true(self):
        self.assertTrue(show_school_in_daily_summary({}))

    def test_exclude_when_off(self):
        cfg = {
            "show_school_in_daily_summary": False,
            "school_calendars": ["school"],
        }
        events = [
            {"summary": "Math", "calendar_id": "school"},
            {"summary": "Dinner", "calendar_id": "family"},
        ]
        out = exclude_school_from_schedule(events, cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["summary"], "Dinner")

    def test_no_exclude_when_on(self):
        cfg = {
            "show_school_in_daily_summary": True,
            "school_calendars": ["school"],
        }
        events = [
            {"summary": "Math", "calendar_id": "school"},
            {"summary": "Dinner", "calendar_id": "family"},
        ]
        self.assertEqual(len(exclude_school_from_schedule(events, cfg)), 2)


class TestSchoolCalendarApiSurfaces(unittest.TestCase):
    def test_api_today_filters_schedule_events(self):
        from pathlib import Path
        api_root = Path(__file__).resolve().parents[1] / "api"
        content = "\n".join(p.read_text(encoding="utf-8") for p in sorted(api_root.rglob("*.py")))
        self.assertTrue("exclude_school_from_schedule(events," in content)
        idx = content.find("async def get_today")
        self.assertGreater(idx, -1)
        body = content[idx : idx + 4000]
        self.assertTrue("exclude_school_from_schedule(events," in body)


if __name__ == "__main__":
    unittest.main()
