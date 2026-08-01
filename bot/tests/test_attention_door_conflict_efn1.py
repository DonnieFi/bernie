"""family-bot-efn.1: door + calendar conflict attention signals."""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TODAY_JS = _ROOT / "web" / "static" / "js" / "v3_today.js"
_TODAY_PY = Path(__file__).resolve().parents[1] / "api" / "routes" / "today.py"


class TestAttentionDoorConflictEfn1(unittest.TestCase):
    def test_js_door_and_conflict(self):
        js = _TODAY_JS.read_text(encoding="utf-8")
        self.assertIn("door_alert", js)
        self.assertIn("kind: \"door\"", js)
        self.assertIn("kind: \"conflict\"", js)
        self.assertIn("Schedule crunch", js)
        self.assertIn("Someone at the", js)
        self.assertIn('action: "security"', js)
        self.assertIn('showPanel(panel)', js)

    def test_api_door_alert(self):
        py = _TODAY_PY.read_text(encoding="utf-8")
        self.assertIn("door_alert", py)
        self.assertIn("family-bot-efn.1", py)
        self.assertIn("get_events", py)


if __name__ == "__main__":
    unittest.main()
