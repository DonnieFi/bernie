import os
import sys
import unittest

from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as test_db


class TestCalendarTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        from tools import load_all_domains
        load_all_domains()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old_db_path = test_db.DB_PATH
        test_db.DB_PATH = self._tmp.name
        await test_db.init_db()
        self._writes_local = patch("db_client.writes_locally", return_value=True)
        self._writes_local.start()

        class Ctx:
            shadow = False
            person_id = "person:red"
            group = "parents"
            config = {}
            class services:
                db = test_db
                tz = ZoneInfo("America/Halifax")
                calendar = None
        self.ctx = Ctx()

    async def asyncTearDown(self):
        import os
        self._writes_local.stop()
        test_db.DB_PATH = self._old_db_path
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    async def test_create_event_stores_draft(self):
        from tools.calendar import handle_create_event
        args = {
            "summary": "Dental cleanup",
            "date": "2026-06-01",
            "time": "14:30",
            "duration_minutes": 45,
            "attendees": ["Dad", "Mom"],
            "location": "Halifax Dental",
            "description": "Routine checkup"
        }
        res = await handle_create_event(args, self.ctx)
        self.assertIn("Draft ready for confirmation", res)
        self.assertIn("draft_id=", res)

        # Extract draft ID
        import re
        match = re.search(r"draft_id=([a-zA-Z0-9_]+)", res)
        self.assertIsNotNone(match)
        draft_id = match.group(1)

        # Retrieve the draft using test_db to assert store_draft side effect
        draft = await test_db.get_draft(draft_id)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["summary"], "Dental cleanup")
        self.assertEqual(draft["location"], "Halifax Dental")
        self.assertEqual(draft["description"], "Routine checkup")
        self.assertEqual(draft["attendees"], ["Dad", "Mom"])

    async def test_get_rsvps_with_mocked_calendar_and_db(self):
        from tools.calendar import handle_get_rsvps

        # Mock the calendar service to return matching event
        cal_mock = MagicMock()
        mock_events = [
            {"id": "event123", "summary": "Child1 Soccer Practice", "start": "2026-06-02T18:00:00Z"}
        ]
        cal_mock.get_events_for_days = AsyncMock(return_value=mock_events)
        self.ctx.services.calendar = cal_mock

        # Mock the database service to return custom RSVPs
        db_mock = MagicMock()
        mock_rsvps = [
            {"name": "Dad", "status": "yes"},
            {"name": "Mom", "status": "maybe"},
            {"name": "Child1", "status": "no"}
        ]
        db_mock.get_rsvps = AsyncMock(return_value=mock_rsvps)
        self.ctx.services.db = db_mock

        args = {"event_name": "soccer"}
        res = await handle_get_rsvps(args, self.ctx)

        self.assertIn("RSVPs for Child1 Soccer Practice:", res)
        self.assertIn("✅ Dad", res)
        self.assertIn("🤔 Mom", res)
        self.assertIn("❌ Child1", res)

        cal_mock.get_events_for_days.assert_called_once_with(14)
        db_mock.get_rsvps.assert_called_once_with("event123")

    async def test_calendar_read_tool_falls_back_to_text_when_summary_disabled(self):
        from tools.calendar import handle_get_todays_events

        cal_mock = MagicMock()
        events = [{"id": "event123", "summary": "Soccer"}]
        cal_mock.get_todays_events = AsyncMock(return_value=events)
        cal_mock.events_to_text = MagicMock(return_value="text events")
        cal_mock.events_to_summary = MagicMock(return_value="summary events")
        self.ctx.services.calendar = cal_mock
        self.ctx.config = {"tool_gateway": {"calendar_summary_mode": False}}

        res = await handle_get_todays_events({}, self.ctx)

        self.assertEqual(res, "text events")
        cal_mock.events_to_text.assert_called_once_with(events)
        cal_mock.events_to_summary.assert_not_called()
