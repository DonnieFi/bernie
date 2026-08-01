import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from utils.discord_helpers import weekday_num, next_automation_run

class TestTaskUtils(unittest.TestCase):

    def test_weekday_num(self):
        self.assertEqual(weekday_num("monday"), 0)
        self.assertEqual(weekday_num("tuesday"), 1)
        self.assertEqual(weekday_num("wednesday"), 2)
        self.assertEqual(weekday_num("thursday"), 3)
        self.assertEqual(weekday_num("friday"), 4)
        self.assertEqual(weekday_num("saturday"), 5)
        self.assertEqual(weekday_num("sunday"), 6)

        with self.assertRaises(ValueError):
            weekday_num("notaday")

    def test_next_automation_run(self):
        tz = "America/Halifax"
        base_dt = datetime(2026, 6, 1, 6, 0, 0, tzinfo=ZoneInfo(tz)) # Mon, Jun 1, 2026 @ 6am
        
        # daily at 08:00
        run_daily = next_automation_run("daily", {"time": "08:00"}, tz, after_dt=base_dt)
        self.assertIsNotNone(run_daily)
        self.assertEqual(run_daily.hour, 8)
        self.assertEqual(run_daily.minute, 0)
        self.assertEqual(run_daily.day, 1)

    def test_next_automation_run_rejects_invalid_daily_time(self):
        tz = "America/Halifax"
        base_dt = datetime(2026, 6, 1, 6, 0, 0, tzinfo=ZoneInfo(tz))
        with self.assertRaises(ValueError):
            next_automation_run("daily", {"time": "25:00"}, tz, after_dt=base_dt)

if __name__ == "__main__":
    unittest.main()
