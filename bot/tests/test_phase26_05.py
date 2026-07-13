"""Tests for Phase 26-05 — Cost reporting + Cognition panel."""
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.modules.setdefault("discord", MagicMock())
sys.modules.setdefault("discord.ext", MagicMock())
sys.modules.setdefault("discord.ext.tasks", MagicMock())
# NOTE: do NOT stub `anthropic` here — it leaks into the combined unittest run
# and breaks tests.test_eval_service.test_routes_claude_to_anthropic (which needs
# the real pydantic_ai.models.anthropic). The real anthropic package imports
# cheaply and creates no client at import time, so no stub is needed.

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import sqlite_async
import sqlite3

from tests.utils import _bot, _web

try:
    import sqlite_async
    import database as db
except ModuleNotFoundError:
    db = None


@unittest.skipUnless(db is not None, "database not available")
class TestCognitionQueries(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "cog05.db")
        await db.init_db()
        await db.create_cognitive_task(type="reflection", payload={"x": 1})
        await db.create_cognitive_task(type="study_guide", payload={"x": 2})
        await db.create_cognitive_task(type="research", payload={"x": 3})

    async def asyncTearDown(self):
        if hasattr(db, 'close_db'):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_get_cognitive_runs(self):
        rows = await db.get_cognitive_runs(limit=10)
        self.assertEqual(len(rows), 3)
        types = {r["type"] for r in rows}
        self.assertEqual(types, {"reflection", "study_guide", "research"})

    async def test_get_cognitive_runs_filtered(self):
        rows = await db.get_cognitive_runs(limit=10, task_type="research")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "research")

    async def test_get_cognitive_stats(self):
        stats = await db.get_cognitive_stats(days=7)
        type_set = {s["type"] for s in stats}
        self.assertEqual(type_set, {"reflection", "study_guide", "research"})

    async def test_get_cognitive_outputs(self):
        await db.store_task_output(1, "research:1", "hello body")
        rows = await db.get_cognitive_outputs(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "research:1")
        self.assertEqual(rows[0]["content_len"], 10)


class TestWeeklyDigestBuilder(unittest.TestCase):
    def test_build_summary_renders_rows(self):
        from eval_service import build_weekly_cognitive_summary
        stats = [
            {"type": "reflection", "runs": 7, "done": 7, "failed": 0,
             "avg_duration_ms": 18000, "total_tokens_in": 4200, "total_tokens_out": 800},
            {"type": "research", "runs": 3, "done": 2, "failed": 1,
             "avg_duration_ms": 240000, "total_tokens_in": 12000, "total_tokens_out": 3000},
        ]
        text = build_weekly_cognitive_summary(stats, days=7)
        self.assertIn("reflection", text)
        self.assertIn("research", text)
        self.assertIn("100%", text)
        self.assertIn("66%", text)  # 2/3

    def test_build_summary_empty(self):
        from eval_service import build_weekly_cognitive_summary
        text = build_weekly_cognitive_summary([], days=7)
        self.assertIn("No cognitive worker runs", text)


class TestCacheBusterAndPanelWired(unittest.TestCase):
    def test_nav_ia_six_items(self):
        app_js = _web("static", "js", "app.v6.js").read_text()
        self.assertIn('label: "Plan"', app_js)
        self.assertIn('label: "People"', app_js)
        self.assertIn('label: "Security"', app_js)
        self.assertIn('label: "Admin"', app_js)
        self.assertNotIn('label: "Chat @ Nano"', app_js)
        self.assertNotIn('id: "cognition"', app_js)

    def test_nav_semantic_buttons_and_mobile_more(self):
        app_js = _web("static", "js", "app.v6.js").read_text()
        self.assertIn('type: "button"', app_js)
        self.assertIn("aria-current", app_js)
        self.assertIn("nav-more", app_js)
        self.assertIn("shellNavGroups", app_js)

    def test_family_name_wired(self):
        auth_src = _bot("api", "routes", "auth.py").read_text()
        self.assertIn('"family_name"', auth_src)
        app_js = _web("static", "js", "app.v6.js").read_text()
        self.assertIn("applyBranding", app_js)
        self.assertIn("family_name", app_js)

    def test_orphan_css_removed(self):
        css_dir = _web("static", "css")
        for name in ("ask.css", "base.css", "chat.css", "shell.css", "today.css"):
            self.assertFalse((css_dir / name).exists(), name)

    def test_usage_dashboard_uses_v3_tokens(self):
        v3 = _web("static", "css", "v3.css").read_text()
        self.assertIn("var(--bg-app)", v3)
        self.assertNotIn("#f1e6ce", v3)

    def test_cache_buster_v75(self):
        idx = _web("index.html").read_text()
        self.assertIn("app.v6.js?v=75", idx)
        self.assertNotIn("?v=25", idx)

    def test_load_panel_data_gated_on_navigation(self):
        app_js = _web("static", "js", "app.v6.js").read_text()
        self.assertIn("prevPanel !== id", app_js)
        self.assertIn("function getPanels()", app_js)

    def test_admin_logs_cleanup_wired(self):
        admin_js = _web("static", "js", "v3_admin.js").read_text()
        app_js = _web("static", "js", "app.v6.js").read_text()
        self.assertIn("v3AdminLeave", admin_js)
        self.assertIn("v3AdminLeave", app_js)
        self.assertNotIn('_activePanel === "logs"', app_js)

    def test_home_panel_data_split(self):
        app_js = _web("static", "js", "app.v6.js").read_text()
        self.assertIn("refreshHomeData", app_js)
        self.assertIn("refreshTodayData", app_js)
        self.assertIn("syncPanelVisibility", app_js)

    def test_admin_prefetch(self):
        app_js = _web("static", "js", "app.v6.js").read_text()
        self.assertIn("refreshAdminData", app_js)
        self.assertIn("/api/usage?days=30", app_js)
        self.assertIn("/api/activity/notifications", app_js)

    def test_cognition_panel_script_removed(self):
        idx = _web("index.html").read_text()
        self.assertNotIn("v3_cognition.js", idx)

    def test_family_bootstrap_prefetch(self):
        app_js = _web("static", "js", "app.v6.js").read_text()
        self.assertIn("loadFamilyBootstrap", app_js)
        self.assertIn("/api/home/dashboard", app_js)
        self.assertIn("/api/rooms", app_js)
        self.assertIn("renderHome(true)", app_js)

    def test_security_panel_active_id(self):
        cam_js = _web("static", "js", "v3_cameras.js").read_text()
        self.assertIn("isSecurityPanelActive", cam_js)
        self.assertIn('"security"', cam_js)
        self.assertNotIn('_activePanel !== "cameras"', cam_js)


class TestAPIRoutesPresent(unittest.TestCase):
    """Cognition API kept for ops; dashboard panel removed in phase-41."""

    def test_routes_exist(self):
        src = "\n".join(p.read_text() for p in sorted(_bot("api").rglob("*.py")))
        self.assertIn('@router.get("/api/cognition/runs"', src)
        self.assertIn('@router.get("/api/cognition/outputs"', src)
        self.assertIn('@router.get("/api/cognition/stats"', src)
        self.assertIn("_require_admin_or_parents", src)


if __name__ == "__main__":
    unittest.main()
