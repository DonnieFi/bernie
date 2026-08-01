"""Settings UI exposes subscription model provider labels (static contract)."""
from __future__ import annotations

import unittest
from pathlib import Path


class TestV3SettingsSubscription(unittest.TestCase):
    def test_model_rows_show_oauth_and_runner_status(self):
        js = (Path(__file__).resolve().parents[2] / "web" / "static" / "js" / "v3_settings.js").read_text(
            encoding="utf-8",
        )
        self.assertIn("subscription_providers", js)
        self.assertIn("Subscription OAuth (runner)", js)
        self.assertIn('authLabel(prov)', js)
        self.assertIn("formatModelOption", js)
        self.assertIn("current_fallback_chain", js)
        self.assertIn("grok", js)
        self.assertIn("codex", js)


if __name__ == "__main__":
    unittest.main()
