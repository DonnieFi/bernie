"""family-bot-tsi: kill Command Center + Discord/model chrome on family shell."""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HTML = _ROOT / "web" / "index.html"
_APP = _ROOT / "web" / "static" / "js" / "app.v6.js"


class TestTsiFamilyChrome(unittest.TestCase):
    def test_login_no_command_center(self):
        html = _HTML.read_text(encoding="utf-8")
        self.assertNotIn("Command Center", html)
        self.assertIn("Who's using the tablet?", html)
        self.assertIn("bernie", html.lower())

    def test_operator_chrome_admin_only(self):
        js = _APP.read_text(encoding="utf-8")
        self.assertIn("family-bot-tsi", js)
        self.assertIn('role === "admin"', js)
        self.assertIn("DISCORD · CONNECTED", js)
        self.assertIn("data-operator-chrome", js)
        # Family path uses family name + calm copy, not telemetry
        self.assertIn("bernie is here · ask anytime", js)
        self.assertIn("famLabel", js)

    def test_parents_not_operator_chrome(self):
        """Parents share isAdminUser but must not see Discord/model strip."""
        js = _APP.read_text(encoding="utf-8")
        # Gate is true-admin role, not isAdminUser()
        block = js[js.find("family-bot-tsi") : js.find("family-bot-tsi") + 900]
        self.assertIn("isTrueAdmin", block)
        self.assertNotIn("isAdminUser()", block)


if __name__ == "__main__":
    unittest.main()
