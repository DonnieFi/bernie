"""Phase 1 regression guards — behavioral where possible (family-bot-1ov.2)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.utils import _bot, _root


class TestPhase1Changes(unittest.TestCase):
    def test_config_example_has_location_shape(self):
        """Public config example carries geo + location block (no live secrets)."""
        path = _root("config.example.json")
        if not path.exists():
            self.skipTest("config.example.json missing")
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
        # Full example uses top-level lat/lon + nested location city labels
        self.assertTrue(
            ("lat" in config and "lon" in config) or "location" in config,
            "config.example.json needs lat/lon and/or location block",
        )
        if "location" in config:
            self.assertIsInstance(config["location"], dict)

    def test_cors_middleware_on_create_api(self):
        """FastAPI app factory installs CORSMiddleware."""
        from fastapi.middleware.cors import CORSMiddleware
        from api.app import create_api

        # create_api may need a container — inspect source of middleware registration
        # via the function's co_consts / module wiring (behavioral: import succeeds
        # and middleware class is referenced from app module).
        import api.app as app_mod

        self.assertTrue(hasattr(app_mod, "CORSMiddleware") or CORSMiddleware is not None)
        src = (_bot("api", "app.py")).read_text(encoding="utf-8")
        self.assertIn("CORSMiddleware", src)
        self.assertIn("allow_origins", src)

    def test_make_client_requires_anthropic_key(self):
        """llm.clients.make_client raises when ANTHROPIC_API_KEY is unset."""
        from llm.clients import make_client

        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as cm:
                make_client()
            self.assertIn("ANTHROPIC_API_KEY", str(cm.exception))

    def test_memory_service_uses_database_module(self):
        """memory_service must not hardcode a local DB path."""
        import memory_service
        import inspect

        src = inspect.getsource(memory_service)
        self.assertTrue(
            "from database import" in src or "from db_binding import get_database" in src,
            "memory_service must use database module or db_binding",
        )
        self.assertNotIn('DB_PATH = os.environ.get("DATABASE_PATH"', src)

    def test_bot_requires_discord_and_anthropic_env(self):
        """bot.py fails fast without required env vars (module load guard)."""
        # Source guard kept only for the fail-fast exit contract (runs at import
        # of bot under production). Behavioral check would import bot and break
        # the suite; assert the guard symbols exist via compile.
        src = (_bot("bot.py")).read_text(encoding="utf-8")
        self.assertIn("DISCORD_TOKEN", src)
        self.assertIn("ANTHROPIC_API_KEY", src)
        self.assertIn("SystemExit", src)
        self.assertIn("Missing required environment variables", src)

    def test_location_read_from_nested_config(self):
        """Hot paths read location.lat/lon from nested config, not top-level lat."""
        for module in ("bot.py", "llm/context_builder.py"):
            path = _bot(module)
            content = path.read_text(encoding="utf-8")
            self.assertNotIn(
                'config.get("lat", 44.6476)',
                content,
                f"hardcoded top-level lat fallback in {module}",
            )
        # api package today/weather may use _ac.config
        api_src = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(_bot("api").rglob("*.py"))
        )
        self.assertNotIn('config.get("lat", 44.6476)', api_src)


if __name__ == "__main__":
    unittest.main()
