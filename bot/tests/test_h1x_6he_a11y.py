"""family-bot-6he: a11y baseline — contrast, 14px, reduced-motion, focus, aria-live."""
from __future__ import annotations

import unittest
from pathlib import Path


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "web" / "static" / "css" / "v3.css").exists():
            return parent
    return here.parents[2]


_ROOT = _repo_root()
_CSS = _ROOT / "web" / "static" / "css" / "v3.css"
_APP = _ROOT / "web" / "static" / "js" / "app.v6.js"


class Test6heA11y(unittest.TestCase):
    def setUp(self):
        self.css = _CSS.read_text(encoding="utf-8")
        self.app = _APP.read_text(encoding="utf-8")

    def test_reduced_motion_in_v3(self):
        self.assertIn("family-bot-6he", self.css)
        self.assertIn("prefers-reduced-motion", self.css)

    def test_ink4_not_too_dark(self):
        # Old failing token was #5a5448
        self.assertNotIn("--ink-4:         #5a5448", self.css)
        self.assertIn("--ink-4:", self.css)

    def test_focus_visible(self):
        self.assertIn(":focus-visible", self.css)
        self.assertIn("outline: 2px solid var(--amber)", self.css)

    def test_body_14px(self):
        self.assertIn("font-size: 14px", self.css)
        self.assertIn("#panel-today", self.css)

    def test_toast_aria_live(self):
        self.assertIn("aria-live", self.app)
        self.assertIn('role: "status"', self.app)


if __name__ == "__main__":
    unittest.main()
