"""family-bot-ngo: Home demote HA ops; elevate rooms/comfort (Living vs Plant)."""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HOME = _ROOT / "web" / "static" / "js" / "v3_home.js"


class TestNgoHomeDemotion(unittest.TestCase):
    def setUp(self):
        self.js = _HOME.read_text(encoding="utf-8")

    def test_no_ha_eyebrow_on_family_path(self):
        self.assertIn("family-bot-ngo", self.js)
        self.assertIn("Around the house", self.js)
        self.assertIn("isHomePlantAdmin", self.js)
        # Eyebrow default for family is not "Home Assistant"
        self.assertIn('eyebrow = isHomePlantAdmin()', self.js)
        self.assertIn("data-home-eyebrow", self.js)

    def test_plant_layer_gates(self):
        self.assertIn("canSeePlantLayer", self.js)
        self.assertIn("data-home-plant-more", self.js)
        self.assertIn("More · house plant", self.js)

    def test_living_snapshot_without_system_for_family(self):
        # Automations/System only appended for plant admin
        self.assertIn("livingTiles", self.js)
        self.assertIn("plantTiles", self.js)
        self.assertIn("isHomePlantAdmin()", self.js)

    def test_system_strip_admin_only(self):
        # buildSystemHealthStrip returns null unless plant admin
        idx = self.js.find("const buildSystemHealthStrip")
        block = self.js[idx : idx + 400]
        self.assertIn("isHomePlantAdmin", block)


if __name__ == "__main__":
    unittest.main()
