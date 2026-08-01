"""family-bot-3rl: Plan parent approve strip + Today tap-through."""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TASKS = _ROOT / "web" / "static" / "js" / "v3_tasks.js"
_TODAY = _ROOT / "web" / "static" / "js" / "v3_today.js"
_CSS = _ROOT / "web" / "static" / "css" / "v3_tasks.css"


class TestApproveStrip3rl(unittest.TestCase):
    def test_plan_approve_strip(self):
        js = _TASKS.read_text(encoding="utf-8")
        self.assertIn("function pendingApprovalTasks", js)
        self.assertIn("function approveStripHtml", js)
        self.assertIn("plan-approve-strip", js)
        self.assertIn("approveStripHtml()", js)
        self.assertIn("data-approve-yes", js)
        self.assertIn("data-approve-no", js)
        self.assertIn('approved: true', js)
        self.assertIn('approved: false', js)

    def test_today_approval_taps_plan(self):
        js = _TODAY.read_text(encoding="utf-8")
        self.assertIn("pri: 11", js)
        self.assertIn('showPanel("plan")', js)
        self.assertIn("tap to review", js)

    def test_css_exists(self):
        css = _CSS.read_text(encoding="utf-8")
        self.assertIn("approve-strip", css)
        self.assertIn("family-bot-3rl", css)


if __name__ == "__main__":
    unittest.main()
