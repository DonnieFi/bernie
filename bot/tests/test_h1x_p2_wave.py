"""family-bot-h1x P2: b7w rename, dn1 ask chrome, igj disclosure, 2fe tokens."""
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
_TODAY = _ROOT / "web" / "static" / "js" / "v3_today.js"
_FAM = _ROOT / "web" / "static" / "js" / "v3_family.js"
_V3 = _ROOT / "web" / "static" / "css" / "v3.css"
_TOKENS = _ROOT / "web" / "static" / "css" / "tokens.css"


class TestH1xP2(unittest.TestCase):
    def test_b7w_nav_labels(self):
        js = _APP.read_text(encoding="utf-8")
        self.assertIn("family-bot-b7w", js)
        self.assertIn('label: "Family"', js)
        self.assertIn('label: "Door"', js)
        # legacy hash ids preserved
        self.assertIn('id: "people"', js)
        self.assertIn('id: "security"', js)
        today = _TODAY.read_text(encoding="utf-8")
        self.assertIn("Open Door", today)
        self.assertNotIn("Open Security", today)

    def test_dn1_no_model_latency_on_ask(self):
        js = _TODAY.read_text(encoding="utf-8")
        self.assertIn("family-bot-dn1", js)
        self.assertNotIn("via ${res?.model", js)
        self.assertNotIn("latency_ms", js)

    def test_igj_progressive_disclosure(self):
        js = _FAM.read_text(encoding="utf-8")
        self.assertIn("family-bot-igj", js)
        self.assertIn("data-family-orb", js)
        self.assertIn("data-family-more", js)
        self.assertIn("showGps", js)

    def test_2fe_tokens_removed(self):
        self.assertFalse(_TOKENS.exists(), "dead tokens.css should be deleted")
        css = _V3.read_text(encoding="utf-8")
        self.assertIn("family-bot-2fe", css)
        self.assertIn("SINGLE production design-token", css)


if __name__ == "__main__":
    unittest.main()
