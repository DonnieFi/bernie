"""family-bot-5vw: get_http_session role-aware fallback."""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

sys.modules.setdefault("aiohttp", MagicMock())

# Avoid pulling llm.clients → anthropic when get_http_session imports llm.runtime.
if "llm.runtime" not in sys.modules:
    _llm = types.ModuleType("llm")
    _runtime = types.ModuleType("llm.runtime")
    _runtime.get_container = MagicMock(return_value=None)
    sys.modules["llm"] = _llm
    sys.modules["llm.runtime"] = _runtime


class TestHttpSessionRoleFallback(unittest.TestCase):
    def test_api_role_raises_without_container_session(self):
        from http_session import get_http_session
        import llm.runtime as rt

        env = {k: v for k, v in os.environ.items() if k != "BERNIE_TESTING"}
        env["ROLE"] = "api"
        rt.get_container = MagicMock(return_value=None)
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                get_http_session()
        self.assertIn("ROLE=", str(ctx.exception))

    def test_cognition_role_raises_without_container_session(self):
        from http_session import get_http_session
        import llm.runtime as rt

        env = {k: v for k, v in os.environ.items() if k != "BERNIE_TESTING"}
        env["ROLE"] = "cognition"
        rt.get_container = MagicMock(return_value=None)
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                get_http_session()

    def test_discord_falls_back_to_bot_session(self):
        from http_session import get_http_session
        import llm.runtime as rt

        fake_session = MagicMock()
        fake_session.closed = False
        fake_bot = types.ModuleType("bot")
        fake_bot.get_session = MagicMock(return_value=fake_session)
        env = {k: v for k, v in os.environ.items() if k != "BERNIE_TESTING"}
        env["ROLE"] = "discord"
        rt.get_container = MagicMock(return_value=None)
        with patch.dict(os.environ, env, clear=True), \
             patch.dict(sys.modules, {"bot": fake_bot}):
            got = get_http_session()
        self.assertIs(got, fake_session)
        fake_bot.get_session.assert_called_once()
