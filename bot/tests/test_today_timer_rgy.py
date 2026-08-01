"""family-bot-rgy: Today timer and temps poll only when panel active."""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_APP = _ROOT / "web" / "static" / "js" / "app.v6.js"
_HOME = _ROOT / "web" / "static" / "js" / "v3_home.js"


class TestTodayTimerRgy(unittest.TestCase):
    def test_today_interval_guards_active_panel(self):
        js = _APP.read_text(encoding="utf-8")
        self.assertIn("family-bot-rgy", js)
        self.assertIn('_activePanel !== "today"', js)
        self.assertIn("window.renderToday", js)

    def test_temps_poll_skips_off_panel_home(self):
        js = _HOME.read_text(encoding="utf-8")
        self.assertIn("family-bot-rgy", js)
        self.assertIn('_activePanel === "home"', js)
        self.assertIn("window.renderHome", js)


if __name__ == "__main__":
    unittest.main()
