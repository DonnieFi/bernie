"""family-bot-1bf.1: shared session + internal POST must use ClientTimeout."""

from __future__ import annotations

import ast
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_BOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestTimeoutSourceGates(unittest.TestCase):
    """Source-level gates — no real aiohttp required (sibling tests mock it)."""

    def test_main_uses_make_shared_session(self):
        path = os.path.join(_BOT_DIR, "main.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("make_shared_session", src)
        self.assertIn("from http_session import make_shared_session", src)
        # No bare ClientSession() in main after 1bf.1
        tree = ast.parse(src)
        bare = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = None
                if isinstance(func, ast.Attribute) and func.attr == "ClientSession":
                    name = "ClientSession"
                elif isinstance(func, ast.Name) and func.id == "ClientSession":
                    name = "ClientSession"
                if name == "ClientSession" and not node.keywords and not node.args:
                    bare.append(node.lineno)
        self.assertEqual(bare, [], f"bare ClientSession() still in main.py lines {bare}")

    def test_http_session_defines_defaults(self):
        path = os.path.join(_BOT_DIR, "http_session.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("DEFAULT_CLIENT_TIMEOUT", src)
        self.assertIn("INTERNAL_POST_TIMEOUT", src)
        self.assertIn("def make_shared_session", src)
        self.assertIn("timeout=DEFAULT_CLIENT_TIMEOUT", src)

    def test_cross_container_post_uses_internal_timeout(self):
        path = os.path.join(_BOT_DIR, "cross_container.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("INTERNAL_POST_TIMEOUT", src)
        self.assertIn("timeout=INTERNAL_POST_TIMEOUT", src)


class TestPostJsonTimeoutKwarg(unittest.IsolatedAsyncioTestCase):
    async def test_post_json_passes_timeout(self):
        # cross_container may need a stub aiohttp if the env has none
        if "aiohttp" not in sys.modules:
            sys.modules["aiohttp"] = MagicMock()
        from cross_container import _post_json
        from http_session import INTERNAL_POST_TIMEOUT

        ok_resp = AsyncMock()
        ok_resp.status = 200
        ok_resp.json = AsyncMock(return_value={"message_id": 42})
        ok_resp.__aenter__ = AsyncMock(return_value=ok_resp)
        ok_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=ok_resp)

        with patch.dict(os.environ, {"INTERNAL_POST_SECRET": "test-secret"}), \
             patch("cross_container.get_http_session", return_value=session):
            data = await _post_json("http://example/internal/post", {"channel_id": 1})

        self.assertEqual(data["message_id"], 42)
        _args, kwargs = session.post.call_args
        self.assertIn("timeout", kwargs)
        self.assertIs(kwargs["timeout"], INTERNAL_POST_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
