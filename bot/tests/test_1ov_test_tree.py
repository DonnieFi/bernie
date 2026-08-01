"""family-bot-1ov.1: dual test trees consolidated — bot/tests is sole discover root."""
from __future__ import annotations

import unittest
from pathlib import Path


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "tests" / "gate_manifest.txt").exists():
            return p
        if (p / "gate_manifest.txt").exists() and p.name == "tests":
            return p.parent
    return here.parents[2]


def _bot_tests_dir() -> Path:
    """Canonical unittest dir: bot/tests on host, /app/tests in container mount."""
    here = Path(__file__).resolve().parent
    if here.name == "tests" and (here / "test_1ov_test_tree.py").exists():
        return here
    root = _repo_root()
    for cand in (root / "bot" / "tests", root / "tests"):
        if cand.is_dir() and any(cand.glob("test_*.py")):
            return cand
    return here


class Test1ov1SingleTree(unittest.TestCase):
    def test_root_tests_dir_has_no_test_modules(self):
        # On host: repo/tests/ is manifest-only. In container, /app/tests IS bot/tests.
        root = _repo_root()
        legacy = root / "tests"
        if not legacy.is_dir():
            self.skipTest("no top-level tests/ dir in this layout")
        # If this test file lives under that tree, it is the canonical mount — skip
        if Path(__file__).resolve().is_relative_to(legacy.resolve()):
            self.skipTest("container maps bot/tests → tests/; dual-tree check is host-only")
        py = [p for p in legacy.glob("test_*.py")]
        self.assertEqual(py, [], f"legacy tests/ still has modules: {py}")

    def test_gate_manifest_root_tests_empty(self):
        root = _repo_root()
        manifest = root / "tests" / "gate_manifest.txt"
        if not manifest.exists():
            manifest = root / "gate_manifest.txt"
        if not manifest.exists():
            self.skipTest("gate_manifest not mounted in this environment")
        text = manifest.read_text(encoding="utf-8")
        self.assertIn("ROOT_TESTS", text)
        after = text.split("# === ROOT_TESTS ===", 1)[-1].split("# ===", 1)[0]
        lines = [
            ln.strip()
            for ln in after.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        self.assertEqual(lines, [], f"ROOT_TESTS should be empty, got {lines}")

    def test_bot_tests_is_canonical(self):
        bot_tests = _bot_tests_dir()
        self.assertTrue(bot_tests.is_dir())
        self.assertTrue(any(bot_tests.glob("test_*.py")))


if __name__ == "__main__":
    unittest.main()
