"""family-bot-8lx.2: api package structure + create_api composition."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
import tempfile

BOT = Path(__file__).resolve().parents[1]


class TestApiPackageStructure(unittest.TestCase):
    def test_package_layout(self):
        api = BOT / "api"
        self.assertTrue(api.is_dir())
        self.assertTrue((api / "app.py").is_file())
        self.assertTrue((api / "common.py").is_file())
        self.assertTrue((api / "routes").is_dir())
        # no flat god-file (package only; monofile must not return)
        self.assertFalse(
            (BOT / "api.py").is_file(),
            "bot/api.py monofile must be removed — package bot/api/ is the surface",
        )
        # composition-only create_api (no dozens of route decorators)
        app_src = (api / "app.py").read_text(encoding="utf-8")
        self.assertIn("include_router", app_src)
        self.assertLess(app_src.count("@app.get"), 2)  # middleware only at most

    def test_create_api_registers_core_paths(self):
        from api import create_api
        c = MagicMock()
        c.db = AsyncMock()
        c.frigate = MagicMock()
        c.notification_orchestrator = MagicMock()
        c.calendar = MagicMock()
        c.weather = MagicMock()
        c.ha = MagicMock()
        c.summary_builder = MagicMock()
        c.connection_manager = MagicMock()
        c.supervisor = MagicMock()
        c.task_store = MagicMock()
        c.unified_tasks = MagicMock()
        c.session = MagicMock()
        web = tempfile.mkdtemp()
        Path(web, "static").mkdir()
        with patch("api.common.WEB_ROOT", web):
            app = create_api(None, c)
        paths = set(app.openapi()["paths"])
        for p in (
            "/api/tasks",
            "/api/config/models",
            "/api/chat",
            "/api/ha/automations",
            "/api/health",
            "/api/logs",
        ):
            self.assertIn(p, paths, f"missing {p}")


if __name__ == "__main__":
    unittest.main()
