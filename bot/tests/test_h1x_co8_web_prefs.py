"""family-bot-co8: config-drive favorites/names (no Example hardcodes)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "web" / "static" / "js" / "app.v6.js").exists():
            return parent
        if (parent / "config.example.json").exists():
            return parent
    return here.parents[2]


_ROOT = _repo_root()
_TODAY = _ROOT / "web" / "static" / "js" / "v3_today.js"
_HOME = _ROOT / "web" / "static" / "js" / "v3_home.js"
_APP = _ROOT / "web" / "static" / "js" / "app.v6.js"
_EXAMPLE = _ROOT / "config.example.json"


class TestCo8WebPrefs(unittest.TestCase):
    def test_prefs_from_quick_action_flag(self):
        from web_ui_prefs import web_ui_prefs

        ui = web_ui_prefs({
            "family_name": "The Smiths",
            "home_assistant": {
                "entities": [
                    {"entity_id": "light.desk_lamp", "name": "Desk Lamp", "quick_action": True},
                    {"entity_id": "light.other", "name": "Other"},
                ]
            },
        })
        self.assertEqual(ui["family_name"], "The Smiths")
        self.assertEqual(len(ui["quick_lights"]), 1)
        self.assertEqual(ui["quick_lights"][0]["id"], "desk-lamp")
        self.assertEqual(ui["quick_lights"][0]["name"], "Desk Lamp")

    def test_prefs_explicit_list_and_default_family(self):
        from web_ui_prefs import web_ui_prefs

        ui = web_ui_prefs({
            "web": {"quick_lights": ["light.porch", "kitchen-lamp"]},
            "home_assistant": {"entities": [
                {"entity_id": "light.porch", "name": "Porch"},
            ]},
        })
        self.assertEqual(ui["family_name"], "Family")
        ids = [x["id"] for x in ui["quick_lights"]]
        self.assertEqual(ids, ["porch", "kitchen-lamp"])

    def test_js_no_dad_lamp_hardcode(self):
        js = _TODAY.read_text(encoding="utf-8")
        self.assertIn("family-bot-co8", js)
        self.assertNotIn('l.id === "dad-lamp"', js)
        self.assertNotIn("/api/lights/dad-lamp", js)
        self.assertNotIn('"Dad"', js)
        self.assertIn("friend", js)
        self.assertIn("quick_lights", js)

    def test_family_consumes_person_colors(self):
        fam = (_ROOT / "web" / "static" / "js" / "v3_family.js").read_text(encoding="utf-8")
        self.assertIn("personAccent", fam)
        self.assertIn("person_colors", fam)
        self.assertNotIn("const PERSON_ACCENTS = {", fam)

    def test_js_air_quality_not_hardcoded_rooms(self):
        js = _HOME.read_text(encoding="utf-8")
        self.assertIn("family-bot-co8", js)
        self.assertNotIn('["Master Bedroom", "Air Quality"]', js)
        self.assertIn("air_quality_rooms", js)

    def test_app_family_default(self):
        js = _APP.read_text(encoding="utf-8")
        self.assertIn('"Family"', js)
        self.assertNotIn('"Example"', js)

    def test_example_has_web_section(self):
        if not _EXAMPLE.exists():
            self.skipTest("config.example.json not mounted in this environment")
        text = _EXAMPLE.read_text(encoding="utf-8")
        self.assertIn('"web"', text)
        self.assertIn("quick_lights", text)


if __name__ == "__main__":
    unittest.main()
