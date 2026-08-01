"""family-bot-10y: login a11y — labels, numeric PIN, dialog role."""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HTML = _ROOT / "web" / "index.html"
_APP = _ROOT / "web" / "static" / "js" / "app.v6.js"


class TestLoginA11y10y(unittest.TestCase):
    def test_dialog_and_pin(self):
        html = _HTML.read_text(encoding="utf-8")
        self.assertIn('role="dialog"', html)
        self.assertIn("aria-modal", html)
        self.assertIn("inputmode=\"numeric\"", html)
        self.assertIn('for="login-token"', html)
        self.assertIn("Who's using the tablet?", html)
        self.assertIn('role="alert"', html)
        self.assertNotIn("Command Center", html)

    def test_avatar_aria(self):
        js = _APP.read_text(encoding="utf-8")
        self.assertIn("family-bot-10y", js)
        self.assertIn("Log in as", js)
        self.assertIn("aria-label", js)


if __name__ == "__main__":
    unittest.main()
