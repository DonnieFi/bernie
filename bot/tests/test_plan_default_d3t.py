"""family-bot-d3t: Plan default = My chores; board secondary; HUD gated."""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TASKS = _ROOT / "web" / "static" / "js" / "v3_tasks.js"
_APP = _ROOT / "web" / "static" / "js" / "app.v6.js"


class TestPlanDefaultD3t(unittest.TestCase):
    def test_default_lens_is_mine(self):
        js = _TASKS.read_text(encoding="utf-8")
        self.assertIn('lens: "mine"', js)
        self.assertIn("LENS_KEY", js)
        self.assertIn("function setLens", js)
        self.assertIn("function applyLensFilters", js)
        self.assertIn('S.fType = "chore"', js)
        self.assertIn("ensurePlanDefaults", js)

    def test_lens_seg_has_mine_household_board(self):
        js = _TASKS.read_text(encoding="utf-8")
        self.assertIn('data-lens="${v}"', js)
        self.assertIn('["mine", "Mine"]', js)
        self.assertIn('["household", "Household"]', js)
        self.assertIn('["board", "Board"]', js)

    def test_hud_gated_for_kids(self):
        js = _TASKS.read_text(encoding="utf-8")
        # getMode forces calm for non-admin
        self.assertIn("if (!isAdminUser()) return \"calm\"", js)
        # h key only for admin
        self.assertIn('e.key === "h" && isAdminUser()', js)
        # setMode refuses hud for kids
        self.assertIn('if (v === "hud" && !isAdminUser()) v = "calm"', js)

    def test_nav_badge_still_my_chores(self):
        app = _APP.read_text(encoding="utf-8")
        self.assertIn("function countMyOpenChores()", app)
        self.assertIn('counts.plan = openChores > 0 ? String(openChores) : ""', app)


if __name__ == "__main__":
    unittest.main()
