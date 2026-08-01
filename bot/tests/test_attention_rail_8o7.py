"""family-bot-8o7: Today thin Attention / Needs you strip."""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TODAY = _ROOT / "web" / "static" / "js" / "v3_today.js"


class TestAttentionRail8o7(unittest.TestCase):
    def test_collect_and_build_exist(self):
        js = _TODAY.read_text(encoding="utf-8")
        self.assertIn("function collectAttentionItems", js)
        self.assertIn("function buildAttentionStrip", js)
        self.assertIn("today-attention", js)
        self.assertIn("today-attention-wrap", js)
        self.assertIn("Needs you", js)
        self.assertIn("Nothing needs you", js)

    def test_real_signals_only(self):
        js = _TODAY.read_text(encoding="utf-8")
        self.assertIn("stormy", js)
        self.assertIn("D.garbage", js)
        self.assertIn("blocked", js)
        self.assertIn("requires_approval", js)
        # Cap
        self.assertIn("slice(0, 3)", js)
        # No tomorrow-clock false positives; bins only today/tomorrow
        self.assertIn("isTomorrowSched", js)
        self.assertIn("days_until", js)
        self.assertIn("imminent", js)

    def test_strip_above_narrative(self):
        js = _TODAY.read_text(encoding="utf-8")
        # attention after header; yxz moved Ask below grid
        self.assertIn(
            "main.append(headerWrap, attentionWrap, gridWrap, askWrap, quickWrap)",
            js,
        )
        self.assertIn("buildAttentionStrip(D)", js)


if __name__ == "__main__":
    unittest.main()
