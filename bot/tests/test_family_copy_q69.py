"""family-bot-q69: family-facing copy pass."""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CAM = _ROOT / "web" / "static" / "js" / "v3_cameras.js"
_TASKS = _ROOT / "web" / "static" / "js" / "v3_tasks.js"
_APP = _ROOT / "web" / "static" / "js" / "app.v6.js"
_HOME = _ROOT / "web" / "static" / "js" / "v3_home.js"


class TestFamilyCopyQ69(unittest.TestCase):
    def test_door_modes_labels(self):
        js = _CAM.read_text(encoding="utf-8")
        self.assertIn("Watching", js)
        self.assertIn("Quiet", js)
        self.assertIn("Practice", js)
        self.assertIn("family-bot-q69", js)
        # API values still on/off/test
        self.assertIn("setMode(m.id)", js)
        self.assertIn("All quiet. That's good.", js)
        self.assertNotIn('"On")', js)  # old chip labels
        self.assertNotIn("No recent motion events.", js)

    def test_empty_kanban_human(self):
        js = _TASKS.read_text(encoding="utf-8")
        self.assertIn("Nothing here right now.", js)
        self.assertNotIn("— none —", js)

    def test_sidebar_operator_chrome_admin_only(self):
        # family-bot-tsi tightened gate to true admin (not parents)
        js = _APP.read_text(encoding="utf-8")
        self.assertIn("family-bot-tsi", js)
        self.assertIn("bernie is here", js)
        self.assertIn("DISCORD · CONNECTED", js)
        self.assertIn('role === "admin"', js)
        self.assertIn("data-operator-chrome", js)

    def test_home_empty_not_config_for_family(self):
        js = _HOME.read_text(encoding="utf-8")
        self.assertIn("Nothing showing yet", js)
        self.assertIn("The house is quiet on this screen", js)


if __name__ == "__main__":
    unittest.main()
