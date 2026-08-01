"""family-bot-cib: chat_general overlaps memory + live context preflight."""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_CHAT = Path(__file__).resolve().parents[1] / "llm" / "chat.py"


class TestChatPreflightCib(unittest.TestCase):
    def test_gather_memory_and_context(self):
        src = _CHAT.read_text(encoding="utf-8")
        self.assertIn("family-bot-cib", src)
        self.assertIn("_fetch_memory", src)
        self.assertIn("_fetch_live", src)
        self.assertIn("import asyncio as _aio", src)
        self.assertIn("await _aio.gather(_fetch_memory(), _fetch_live())", src)

    def test_chat_general_still_builds_bernie_context(self):
        src = _CHAT.read_text(encoding="utf-8")
        tree = ast.parse(src)
        found = False
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "chat_general":
                body = ast.get_source_segment(src, node) or ""
                self.assertIn("BernieContext.build", body)
                self.assertIn("gather", body)
                found = True
        self.assertTrue(found)


if __name__ == "__main__":
    unittest.main()
