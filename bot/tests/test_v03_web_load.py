"""family-bot-v03: web load — wze bootstrap, o5j presence debounce, hcd surgical home."""
from __future__ import annotations

import unittest
from pathlib import Path


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "web" / "static" / "js" / "app.v6.js").exists():
            return parent
    return here.parents[2]


_ROOT = _repo_root()
_APP = _ROOT / "web" / "static" / "js" / "app.v6.js"
_HOME = _ROOT / "web" / "static" / "js" / "v3_home.js"
_FAM = _ROOT / "web" / "static" / "js" / "v3_family.js"
_HTML = _ROOT / "web" / "index.html"


class TestV03WebLoad(unittest.TestCase):
    def setUp(self):
        self.app = _APP.read_text(encoding="utf-8")
        self.home = _HOME.read_text(encoding="utf-8")
        self.fam = _FAM.read_text(encoding="utf-8")
        self.html = _HTML.read_text(encoding="utf-8")

    def test_wze_lazy_bootstrap(self):
        self.assertIn("family-bot-wze", self.app)
        self.assertIn("health only on unlock", self.app)
        # no unlock fan-out of all family APIs
        self.assertNotIn("safe(api(\"/api/today\")),", self.app.split("loadInitial")[1][:800] if "loadInitial" in self.app else "")
        self.assertIn("ensureLeaflet", self.app)
        # Leaflet not always loaded in index
        self.assertNotIn("leaflet.js", self.html)
        self.assertIn("Leaflet loaded on demand", self.html)

    def test_wze_admin_network_dedupe(self):
        self.assertIn("forceNetwork", self.app)
        self.assertIn("needNetwork", self.app)

    def test_o5j_presence_debounce(self):
        self.assertIn("family-bot-o5j", self.app)
        self.assertIn("schedulePresenceRender", self.app)
        self.assertIn("250", self.app)  # debounce ms

    def test_hcd_surgical_home(self):
        self.assertIn("family-bot-hcd", self.home)
        self.assertIn("patchHomeLight", self.home)
        self.assertIn("data-light-id", self.home)
        self.assertIn("patchHomeLight", self.app)
        # temp hover no longer full re-render
        self.assertIn("CSS-only", self.home)

    def test_co8_person_accent_residual(self):
        self.assertIn("personAccent", self.fam)
        self.assertIn("person_colors", self.fam)

    def test_6he_door_mode_buttons(self):
        cam = (_ROOT / "web" / "static" / "js" / "v3_cameras.js").read_text(encoding="utf-8")
        self.assertIn('el("button"', cam)
        self.assertIn("mode-chip", cam)
        self.assertIn('type: "button"', cam)


if __name__ == "__main__":
    unittest.main()
