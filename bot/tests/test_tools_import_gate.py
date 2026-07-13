"""Gate: tool domain modules parse/import cleanly (cold-start guard)."""

import ast
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestToolsImportGate(unittest.TestCase):
    def test_tools_transit_syntax(self):
        path = os.path.join(os.path.dirname(__file__), "..", "tools", "transit.py")
        with open(path, encoding="utf-8") as f:
            ast.parse(f.read(), filename=path)

    def test_tools_transit_imports(self):
        importlib.import_module("tools.transit")