"""family-bot-5bu: Today must not silent-empty when weather is missing."""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TODAY_JS = _ROOT / "web" / "static" / "js" / "v3_today.js"
_APP_JS = _ROOT / "web" / "static" / "js" / "app.v6.js"


class TestTodayWeatherMissing5bu(unittest.TestCase):
    def test_no_silent_early_return_on_missing_weather(self):
        content = _TODAY_JS.read_text(encoding="utf-8")
        # The old blank-panel bug: early return before any DOM work.
        self.assertNotIn("if (!D.weather) {\n      return;\n    }", content)
        self.assertNotIn("if (!D.weather) {\n        return;\n    }", content)
        # Compact form also banned.
        self.assertNotRegex(content, r"if\s*\(\s*!D\.weather\s*\)\s*return\s*;")

    def test_missing_weather_card_and_retry_exist(self):
        content = _TODAY_JS.read_text(encoding="utf-8")
        self.assertIn("buildWeatherMissingCard", content)
        self.assertIn("today-weather-missing", content)
        self.assertIn("today-weather-retry", content)
        self.assertIn("Weather is taking a moment", content)
        self.assertIn("window.refreshTodayData", content)
        # Safe sunset access when weather absent.
        self.assertIn("w?.sunset", content)

    def test_build_weather_card_delegates_when_null(self):
        content = _TODAY_JS.read_text(encoding="utf-8")
        self.assertIn("if (!w) return buildWeatherMissingCard();", content)

    def test_refresh_today_data_exported_on_window(self):
        content = _APP_JS.read_text(encoding="utf-8")
        self.assertIn("window.refreshTodayData = refreshTodayData", content)


if __name__ == "__main__":
    unittest.main()
