"""family-bot-yxz: Ask below day narrative on Today."""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TODAY = _ROOT / "web" / "static" / "js" / "v3_today.js"


class TestTodayAskYxz(unittest.TestCase):
    def test_ask_after_grid_in_append(self):
        js = _TODAY.read_text(encoding="utf-8")
        self.assertIn("family-bot-yxz", js)
        # Order: header, attention, grid, ask, quick
        self.assertIn(
            "main.append(headerWrap, attentionWrap, gridWrap, askWrap, quickWrap)",
            js,
        )
        # Must not put ask before grid on create
        self.assertNotIn(
            "main.append(headerWrap, attentionWrap, askWrap, gridWrap, quickWrap)",
            js,
        )
        self.assertIn("insertBefore(gridWrap, askWrap)", js)


if __name__ == "__main__":
    unittest.main()
