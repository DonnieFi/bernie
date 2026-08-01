"""family-bot-nn4: one-tap chore complete on Plan cards."""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TASKS = _ROOT / "web" / "static" / "js" / "v3_tasks.js"
_CSS = _ROOT / "web" / "static" / "css" / "v3.css"
_TASKS_CSS = _ROOT / "web" / "static" / "css" / "v3_tasks.css"
_APP = _ROOT / "web" / "static" / "js" / "app.v6.js"


class TestOneTapCompleteNn4(unittest.TestCase):
    def test_quick_done_wired_on_card(self):
        js = _TASKS.read_text(encoding="utf-8")
        self.assertIn("function canQuickComplete", js)
        self.assertIn("data-quick-done=", js)
        self.assertIn("k-complete-btn", js)
        self.assertIn("function quickComplete", js)
        self.assertIn("/complete", js)
        self.assertIn("Nice one,", js)
        self.assertIn("announceLive", js)
        self.assertIn("aria-live", js)

    def test_click_handler_intercepts_before_open(self):
        js = _TASKS.read_text(encoding="utf-8")
        self.assertIn("data-quick-done", js)
        self.assertIn("d.quickDone", js)
        # stopPropagation so drawer does not open
        self.assertIn("stopPropagation", js)

    def test_touch_target_44(self):
        css = _CSS.read_text(encoding="utf-8") + _TASKS_CSS.read_text(encoding="utf-8")
        self.assertIn("min-width: 44px", css)
        self.assertIn("min-height: 44px", css)
        self.assertIn("k-complete-btn", css)

    def test_flash_has_aria_live(self):
        app = _APP.read_text(encoding="utf-8")
        self.assertIn('"aria-live": "polite"', app)


if __name__ == "__main__":
    unittest.main()
