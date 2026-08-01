"""family-bot-u9m: camera leave lifecycle — stop poll + revoke blobs without monkey-patch."""
from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CAM_JS = _ROOT / "web" / "static" / "js" / "v3_cameras.js"
_APP_JS = _ROOT / "web" / "static" / "js" / "app.v6.js"


class TestCamerasLeaveU9m(unittest.TestCase):
    def test_no_showpanel_monkey_patch(self):
        content = _CAM_JS.read_text(encoding="utf-8")
        self.assertNotIn("const origShowPanel = window.showPanel", content)
        self.assertNotIn("window.showPanel = function", content)

    def test_v3_cameras_leave_hook_exists(self):
        content = _CAM_JS.read_text(encoding="utf-8")
        self.assertIn("window.v3CamerasLeave = function", content)
        self.assertIn("clearInterval(state.refreshInterval)", content)
        self.assertIn("releaseObjectURLs()", content)

    def test_app_calls_cameras_leave_on_navigate_away(self):
        content = _APP_JS.read_text(encoding="utf-8")
        self.assertIn("window.v3CamerasLeave", content)
        self.assertIn('prevPanel === "security"', content)
        self.assertIn('prevPanel === "cameras"', content)

    def test_render_guards_against_rearm_after_leave(self):
        content = _CAM_JS.read_text(encoding="utf-8")
        # After await loadData, must re-check active panel before arming interval
        self.assertIn("await loadData();", content)
        self.assertIn("if (!isSecurityPanelActive())", content)
        # Late snapshot fetches must not leak blobs off-panel
        self.assertIn("if (!isSecurityPanelActive()) {\n      if (url) URL.revokeObjectURL(url);", content)


if __name__ == "__main__":
    unittest.main()
