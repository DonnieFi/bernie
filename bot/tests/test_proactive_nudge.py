import os
import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from proactive_nudge import collect_nudge_candidates


class TestProactiveNudge(unittest.IsolatedAsyncioTestCase):
    def test_morning_routine_emits_nudge(self):
        tz = ZoneInfo("America/Halifax")
        now = datetime(2026, 6, 6, 8, 0, 0, tzinfo=tz)
        routines = [
            {
                "person_id": "child1",
                "name": "Pack lunch",
                "confidence": 0.8,
                "pattern_json": "{}",
            }
        ]
        nudges = collect_nudge_candidates(
            config={"timezone": "America/Halifax"},
            routines=routines,
            tomorrow_rows=[],
            now=now,
        )
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0].source, "routine")
        self.assertIn("Pack lunch", nudges[0].message)

    def test_tomorrow_context_exam_keyword(self):
        tz = ZoneInfo("America/Halifax")
        now = datetime(2026, 6, 6, 8, 30, 0, tzinfo=tz)
        nudges = collect_nudge_candidates(
            config={"timezone": "America/Halifax"},
            routines=[],
            tomorrow_rows=[
                {
                    "person_id": "child1",
                    "summary": "Math exam at 9am tomorrow",
                    "confidence": 0.7,
                }
            ],
            now=now,
        )
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0].source, "tomorrow_context")

    def test_afternoon_silent(self):
        tz = ZoneInfo("America/Halifax")
        now = datetime(2026, 6, 6, 14, 0, 0, tzinfo=tz)
        nudges = collect_nudge_candidates(
            config={"timezone": "America/Halifax"},
            routines=[{"person_id": "child1", "name": "X", "confidence": 0.9}],
            tomorrow_rows=[{"summary": "exam", "confidence": 0.9}],
            now=now,
        )
        self.assertEqual(nudges, [])

    def test_weekday_routine_emitted_only_on_matching_day(self):
        tz = ZoneInfo("America/Halifax")
        # 2026-06-10 is a Wednesday
        wednesday = datetime(2026, 6, 10, 8, 0, 0, tzinfo=tz)
        # 2026-06-11 is a Thursday
        thursday = datetime(2026, 6, 11, 8, 0, 0, tzinfo=tz)

        routines = [
            {
                "person_id": "mom",
                "name": "Mom has running club pickup duty on Wednesdays",
                "confidence": 0.8,
                "pattern_json": "{}",
            }
        ]

        # Should emit on Wednesday
        nudges_wed = collect_nudge_candidates(
            config={"timezone": "America/Halifax"},
            routines=routines,
            tomorrow_rows=[],
            now=wednesday,
        )
        self.assertEqual(len(nudges_wed), 1)

        # Should NOT emit on Thursday
        nudges_thu = collect_nudge_candidates(
            config={"timezone": "America/Halifax"},
            routines=routines,
            tomorrow_rows=[],
            now=thursday,
        )
        self.assertEqual(len(nudges_thu), 0)

    async def test_nudge_suppressed_if_already_sent_or_muted(self):
        from proactive_nudge import _nudge_already_sent_or_muted, Nudge

        class FakeDB:
            async def fetch_activity_since(self, event_type, since_iso):
                # Simulate that a duplicate proactive_nudge activity was logged
                if event_type == "proactive_nudge":
                    return [{"description": "Heads up — `Test Routine` is usually around this time.", "person_id": "mom"}]
                return []

        db = FakeDB()
        nudge = Nudge(
            message="Heads up — `Test Routine` is usually around this time.",
            person_id="mom",
            confidence=0.8,
            source="routine",
        )

        acted_on = await _nudge_already_sent_or_muted(nudge, db)
        self.assertTrue(acted_on)


if __name__ == "__main__":
    unittest.main()
