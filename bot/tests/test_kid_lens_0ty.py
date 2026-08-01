"""family-bot-0ty: kid lens — schedule mine+family; hide admin language."""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_APP = _ROOT / "web" / "static" / "js" / "app.v6.js"
_TODAY = _ROOT / "web" / "static" / "js" / "v3_today.js"
_HOME = _ROOT / "web" / "static" / "js" / "v3_home.js"
_TASKS = _ROOT / "web" / "static" / "js" / "v3_tasks.js"


class TestKidLens0ty(unittest.TestCase):
    def test_is_kid_user_helper(self):
        app = _APP.read_text(encoding="utf-8")
        self.assertIn("function isKidUser()", app)
        self.assertIn("window.isKidUser = isKidUser", app)
        # tsi: family chrome (not Discord telemetry)
        self.assertIn("bernie is here", app)

    def test_today_schedule_filter(self):
        js = _TODAY.read_text(encoding="utf-8")
        self.assertIn("_filterScheduleForLens", js)
        self.assertIn("family-bot-0ty", js)
        self.assertIn('who === "family"', js)

    def test_ask_strips_model_latency_for_family(self):
        # family-bot-dn1: all family Ask views strip model/latency (not kids-only)
        js = _TODAY.read_text(encoding="utf-8")
        self.assertIn("family-bot-dn1", js)
        self.assertNotIn("via ${res?.model", js)
        self.assertIn("just now", js)

    def test_home_soft_copy_and_plant_gates(self):
        # family-bot-ngo replaced 0ty system-strip comment with plant layer
        js = _HOME.read_text(encoding="utf-8")
        self.assertIn("Nothing to control right now", js)
        self.assertIn("isKidUser", js)
        self.assertIn("isHomePlantAdmin", js)
        self.assertIn("canSeePlantLayer", js)

    def test_plan_board_lens_admin_only(self):
        js = _TASKS.read_text(encoding="utf-8")
        self.assertIn("isAdminUser()", js)
        self.assertIn('["mine", "Mine"]', js)
        self.assertIn('["household", "Household"]', js)
        self.assertIn("Board", js)


if __name__ == "__main__":
    unittest.main()
