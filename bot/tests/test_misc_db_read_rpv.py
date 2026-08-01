"""family-bot-rpv: hot misc SELECTs use _db_read."""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_MISC = Path(__file__).resolve().parents[1] / "database" / "misc.py"

_HOT_READS = (
    "get_chat_threads",
    "get_chat_thread_messages",
    "get_routines",
    "get_semantic_observations",
    "get_ha_devices",
    "get_tomorrow_context",
    "count_memory_events_by_person",
    "list_memory_events",
    "get_observations",
)


class TestMiscDbReadRpv(unittest.TestCase):
    def test_hot_reads_use_db_read(self):
        src = _MISC.read_text(encoding="utf-8")
        tree = ast.parse(src)
        found = {}
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name in _HOT_READS:
                body = ast.get_source_segment(src, node) or ""
                found[node.name] = body
                self.assertIn("_db_read", body, node.name)
                self.assertNotIn("async with _db_conn()", body, node.name)
        for name in _HOT_READS:
            self.assertIn(name, found, f"missing {name}")

    def test_read_count_improved(self):
        src = _MISC.read_text(encoding="utf-8")
        self.assertGreaterEqual(src.count("_db_read()"), 30)
        # Still some writes on _db_conn
        self.assertGreater(src.count("_db_conn()"), 10)
        # Import must include _db_read (NameError guard)
        self.assertRegex(src, r"from database\.conn import \([\s\S]*?_db_read")

    def test_db_read_importable(self):
        from database import misc as m
        self.assertTrue(callable(getattr(m, "_db_read", None) or True))
        # Bound name used in module scope
        import database.misc as misc_mod
        self.assertTrue(hasattr(misc_mod, "_db_read") or "_db_read" in dir(misc_mod))
        # Direct: the name must resolve in the module globals
        self.assertIn("_db_read", misc_mod.__dict__)


if __name__ == "__main__":
    unittest.main()
