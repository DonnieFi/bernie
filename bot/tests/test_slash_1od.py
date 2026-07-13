"""family-bot-1od: slash package uses real modules (no exec of _*_src)."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

SLASH = Path(__file__).resolve().parents[1] / "slash"


class TestSlashNoExec(unittest.TestCase):
    def test_no_src_peel_files(self):
        leftovers = list(SLASH.glob("*_src.py"))
        self.assertEqual(leftovers, [], f"legacy peel sources must be gone: {leftovers}")

    def test_domain_modules_have_register_no_exec(self):
        modules = sorted(SLASH.glob("*_cmds.py"))
        self.assertGreaterEqual(len(modules), 6)
        for path in modules:
            src = path.read_text(encoding="utf-8")
            self.assertIn("def register(", src, path.name)
            # no runtime exec(...) — comments mentioning "no exec" are fine
            self.assertIsNone(
                re.search(r"(?<![A-Za-z_])exec\s*\(", src),
                f"{path.name} still calls exec()",
            )
            # class/name _LiveNS must not be defined (comment mentions are ok)
            self.assertIsNone(
                re.search(r"class\s+_LiveNS\b|\b_LiveNS\s*=", src),
                f"{path.name} still defines _LiveNS",
            )
            tree = ast.parse(src)
            self.assertTrue(any(
                isinstance(n, ast.FunctionDef) and n.name == "register"
                for n in tree.body
            ), path.name)

    def test_slash_registry_finds_peeled_commands(self):
        from slash_registry import list_slash_command_names

        names = list_slash_command_names()
        for required in ("task_add", "weather", "temps", "config_summary", "school", "reminders"):
            self.assertIn(required, names)


if __name__ == "__main__":
    unittest.main()
