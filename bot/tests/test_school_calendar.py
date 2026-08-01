"""Tests for school calendar daily-summary toggle + shared schedule projection."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from school_calendar import (
    exclude_school_from_schedule,
    events_to_api_schedule,
    events_to_wall,
    homework_due_events,
    school_calendar_ids,
    select_daily_schedule,
    show_school_in_daily_summary,
    uniform_notes,
)

_TZ = ZoneInfo("America/Halifax")


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
    def test_api_today_uses_shared_schedule_projection(self):
        from pathlib import Path
        api_root = Path(__file__).resolve().parents[1] / "api"
        content = "\n".join(p.read_text(encoding="utf-8") for p in sorted(api_root.rglob("*.py")))
        idx = content.find("async def get_today")
        self.assertGreater(idx, -1)
        body = content[idx : idx + 4000]
        self.assertIn("select_daily_schedule", body)
        self.assertIn("events_to_api_schedule", body)
        self.assertIn("exclude_school_from_schedule", body)

    def test_embed_uses_select_daily_schedule(self):
        from pathlib import Path
        embeds = (Path(__file__).resolve().parents[1] / "ui" / "embeds.py").read_text(encoding="utf-8")
        self.assertIn("select_daily_schedule", embeds)


class TestSelectDailySchedule(unittest.TestCase):
    def _ev(self, summary, hour=None, *, all_day=False, cal="family", attendees=None):
        if all_day:
            start = datetime(2026, 7, 22, 0, 0, tzinfo=_TZ)
        else:
            start = datetime(2026, 7, 22, hour or 9, 0, tzinfo=_TZ)
        return {
            "summary": summary,
            "start": start,
            "end": start,
            "all_day": all_day,
            "calendar_id": cal,
            "attendees": attendees or [],
            "location": "",
        }

    def test_keeps_all_day_when_school_off(self):
        cfg = {"show_school_in_daily_summary": False, "school_calendars": ["school"]}
        events = [
            self._ev("Vacation", all_day=True),
            self._ev("Soccer", hour=17, attendees=["Child1"]),
            self._ev("Math", hour=9, cal="school"),
        ]
        display, school = select_daily_schedule(events, cfg)
        titles = [e["summary"] for e in display]
        self.assertEqual(titles, ["Vacation", "Soccer"])
        self.assertEqual(school, [])

    def test_first_school_class_only_when_school_on(self):
        cfg = {"show_school_in_daily_summary": True, "school_calendars": ["school"]}
        events = [
            self._ev("Math", hour=9, cal="school"),
            self._ev("Science", hour=11, cal="school"),
            self._ev("Dinner", hour=18),
            self._ev("Field trip", all_day=True, cal="school"),
        ]
        display, school = select_daily_schedule(events, cfg)
        titles = [e["summary"] for e in display]
        self.assertEqual(titles, ["Math", "Dinner"])
        self.assertEqual(len(school), 3)

    def test_api_schedule_includes_all_day_and_early_late_who(self):
        cfg = {"show_school_in_daily_summary": False, "school_calendars": ["school"]}
        events = [
            self._ev("Holiday", all_day=True, attendees=["Family"]),
            self._ev("Early swim", hour=6, attendees=["Child1"]),
            self._ev("Late show", hour=23, attendees=["Dad", "Mom"]),
        ]
        display, _ = select_daily_schedule(events, cfg)
        sched = events_to_api_schedule(display, _TZ)
        hours = [b["hour"] for b in sched]
        self.assertEqual(hours[0], "All day")
        flat = [e for b in sched for e in b["events"]]
        by_title = {e["title"]: e for e in flat}
        self.assertEqual(by_title["Holiday"]["time"], "All day")
        self.assertTrue(by_title["Holiday"]["all_day"])
        self.assertEqual(by_title["Early swim"]["who"], "Child1")
        self.assertIn("Dad", by_title["Late show"]["who"])
        # No 8–22 clip
        self.assertIn("Early swim", by_title)
        self.assertIn("Late show", by_title)


class TestCalendarWallProjection(unittest.TestCase):
    def setUp(self):
        self.config = {
            "school_calendars": [{"id": "school", "student": "Child2"}],
            "family_members": {
                "Dad": {"canonical_id": "dad"},
                "Child1": {"canonical_id": "child1"},
            },
        }
        self.day = datetime(2026, 7, 22, tzinfo=_TZ)

    def _event(self, title, *, calendar_id="family", all_day=False, attendees=None):
        return {
            "id": title.lower().replace(" ", "-"),
            "summary": title,
            "start": self.day,
            "end": self.day + timedelta(days=1),
            "due_date": self.day,
            "all_day": all_day,
            "calendar_id": calendar_id,
            "attendees": attendees or [],
            "location": "",
        }

    def test_person_ids_and_school_grid_rule(self):
        events = [
            self._event("Soccer", attendees=["Child1"]),
            self._event("Math", calendar_id="school"),
            self._event("Book report", calendar_id="school", all_day=True),
        ]
        wall = events_to_wall(events, self.config)
        self.assertEqual([event["title"] for event in wall], ["Soccer"])
        self.assertEqual(wall[0]["person_ids"], ["child1"])
        self.assertFalse(wall[0]["is_school"])

    def test_wall_uses_canonical_person_id_for_display_named_calendar_owner(self):
        wall = events_to_wall([self._event("Dentist", attendees=["Dad"])], self.config)
        self.assertEqual(wall[0]["person_ids"], ["dad"])

    def test_wall_includes_gcal_detail_fields(self):
        events = [{
            **self._event("Soccer", attendees=["Child1"]),
            "description": "Bring cleats [remind:60]",
            "organizer_name": "Coach Pat",
            "organizer_email": "coach@example.com",
            "real_attendees": [{"email": "a@example.com", "name": "Alex", "rsvp": "accepted"}],
            "html_link": "https://calendar.google.com/event?eid=abc",
            "status": "confirmed",
        }]
        wall = events_to_wall(events, self.config)
        self.assertEqual(wall[0]["description"], "Bring cleats")
        self.assertEqual(wall[0]["organizer"], "Coach Pat")
        self.assertEqual(wall[0]["attendees"][0]["name"], "Alex")
        self.assertIn("calendar.google.com", wall[0]["html_link"])

    def test_homework_due_excludes_uniform(self):
        events = [
            self._event("Book report", calendar_id="school", all_day=True),
            self._event("PE uniform", calendar_id="school", all_day=True),
        ]
        self.assertEqual(
            [event["summary"] for event in homework_due_events(events, self.config, self.day.date())],
            ["Book report"],
        )

    def test_uniform_pattern_and_student(self):
        notes = uniform_notes(
            [
                self._event("Spirit Day dress", calendar_id="school", all_day=True),
                self._event("Math", calendar_id="school"),
            ],
            self.config,
        )
        self.assertEqual(notes, [{
            "date": "2026-07-22",
            "text": "Spirit Day dress",
            "person_id": "Child2",
        }])


if __name__ == "__main__":
    unittest.main()
