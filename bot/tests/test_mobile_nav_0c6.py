"""family-bot-0c6: mobile nav labels + Plan my-chores badge."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CSS = _ROOT / "web" / "static" / "css" / "v3.css"
_APP = _ROOT / "web" / "static" / "js" / "app.v6.js"


def _mobile_block(css: str) -> str:
    m = re.search(
        r"@media\s*\(\s*max-width:\s*768px\s*\)\s*\{",
        css,
    )
    if not m:
        return ""
    start = m.start()
    # next @media or end of kanban section — take a generous slice
    rest = css[start : start + 3500]
    return rest


class TestMobileNav0c6(unittest.TestCase):
    def test_mobile_shows_nav_labels(self):
        css = _CSS.read_text(encoding="utf-8")
        self.assertIn("family-bot-0c6", css)
        block = _mobile_block(css)
        # Old bug: hide first span (the label)
        self.assertNotIn(".nav-item > span:first-of-type { display: none; }", block)
        self.assertIn(".nav-item .nav-label", block)
        self.assertIn("font-size: 11px", block)

    def test_mobile_touch_targets(self):
        block = _mobile_block(_CSS.read_text(encoding="utf-8"))
        self.assertIn("min-width: 44px", block)
        self.assertIn("min-height: 44px", block)

    def test_mobile_active_amber(self):
        block = _mobile_block(_CSS.read_text(encoding="utf-8"))
        self.assertIn(".nav-item.active", block)
        self.assertIn("var(--amber)", block)

    def test_plan_badge_css(self):
        block = _mobile_block(_CSS.read_text(encoding="utf-8"))
        self.assertIn('[data-panel="plan"] .num.nav-badge', block)

    def test_app_counts_my_open_chores(self):
        js = _APP.read_text(encoding="utf-8")
        self.assertIn("function countMyOpenChores()", js)
        self.assertIn('counts.plan = openChores > 0 ? String(openChores) : ""', js)
        self.assertIn('class: isPlanBadge ? "num nav-badge" : "num"', js)
        self.assertIn('class: "nav-label"', js)
        # Badge updates on task WS
        self.assertIn("updateSidebarCounts()", js)
        self.assertIn('.nav-item[data-panel="plan"] .num', js)
        # Mobile never applies desktop collapsed shell
        self.assertIn("mobileShell", js)
        self.assertIn("max-width: 768px", js)

    def test_mobile_css_overrides_collapsed(self):
        block = _mobile_block(_CSS.read_text(encoding="utf-8"))
        self.assertIn(".sidebar.collapsed", block)
        self.assertIn("width: 100%", block)


if __name__ == "__main__":
    unittest.main()
