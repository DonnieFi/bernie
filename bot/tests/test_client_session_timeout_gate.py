"""family-bot-1bf.8: production code must not construct bare ClientSession()."""

from __future__ import annotations

import ast
import os
import unittest

_BOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Dev utilities / tests may still use bare sessions; gate production modules only.
_SKIP_DIRS = {
    "tests",
    "__pycache__",
    "scratch",
}
_SKIP_FILES = {
    "inspect_tomorrow.py",
    "inspect_tomorrow_realtime.py",
    "api_tester.py",
    "check_ha.py",
}


def _is_client_session_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "ClientSession":
        return True
    if isinstance(func, ast.Name) and func.id == "ClientSession":
        return True
    return False


def _call_has_timeout(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg == "timeout":
            return True
    return False


def _iter_py_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            if name in _SKIP_FILES:
                continue
            yield os.path.join(dirpath, name)


class TestClientSessionTimeoutGate(unittest.TestCase):
    def test_no_bare_client_session_in_production(self):
        bare: list[str] = []
        for path in _iter_py_files(_BOT_DIR):
            rel = os.path.relpath(path, _BOT_DIR)
            if rel.startswith("tests" + os.sep):
                continue
            with open(path, encoding="utf-8") as f:
                try:
                    tree = ast.parse(f.read(), filename=path)
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _is_client_session_call(node):
                    if not _call_has_timeout(node):
                        bare.append(f"{rel}:{node.lineno}")
        self.assertEqual(
            bare,
            [],
            "aiohttp.ClientSession(...) must pass timeout= (family-bot-1bf.8):\n  "
            + "\n  ".join(bare),
        )


if __name__ == "__main__":
    unittest.main()
