"""family-bot-5hy.4 / 5hy.5: hot memory caps + USER_OVERRIDE."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class TestAppendCap(unittest.TestCase):
    def test_append_under_cap(self):
        from memory_docs import append_fact_with_cap

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "context.md"
            p.write_text("hello\n", encoding="utf-8")
            msg, cons = append_fact_with_cap(p, "new fact", max_chars=500)
            self.assertFalse(cons)
            self.assertIn("updated", msg.lower())
            self.assertIn("new fact", p.read_text(encoding="utf-8"))

    def test_consolidate_on_overflow(self):
        from memory_docs import append_fact_with_cap

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "context.md"
            p.write_text("x" * 400 + "\nkeep me\n", encoding="utf-8")
            msg, cons = append_fact_with_cap(p, "brand new", max_chars=200)
            self.assertTrue(cons)
            body = p.read_text(encoding="utf-8")
            self.assertIn("consolidated", body.lower())
            self.assertIn("brand new", body)
            self.assertLessEqual(len(body), 200)

    def test_consolidate_preserves_single_line_tail(self):
        """Single-line tail ending in newline must not become empty after split."""
        from memory_docs import append_fact_with_cap

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "context.md"
            # One long line that forces consolidation; slice is one newline-terminated line
            p.write_text("keep-this-fact-line\n", encoding="utf-8")
            # Force overflow with a small budget relative to growing content
            for i in range(20):
                append_fact_with_cap(p, f"fact-{i}-" + ("y" * 30), max_chars=180)
            body = p.read_text(encoding="utf-8")
            self.assertTrue(body.strip(), "body must not be wiped empty")
            self.assertIn("fact-", body)


class TestUserOverride(unittest.TestCase):
    def test_read_override(self):
        from memory_docs import read_user_override, is_override_path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "USER_OVERRIDE.md").write_text("Never feed the gremlin after midnight\n")
            self.assertIn("gremlin", read_user_override(root, {}))
            self.assertTrue(is_override_path(root / "USER_OVERRIDE.md", root, {}))
            self.assertFalse(is_override_path(root / "context.md", root, {}))


class TestUpdateToolsRefuseOverride(unittest.IsolatedAsyncioTestCase):
    async def test_update_family_uses_cap(self):
        from tools.memory import handle_update_family_context

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "context.md").write_text("a\n")
            ctx = SimpleNamespace(shadow=False, services=SimpleNamespace())
            with (
                patch("tools.memory._docs_root", return_value=root),
                patch("tools.memory._cfg", return_value={"hot_memory": {"context_md_max_chars": 50}}),
            ):
                msg = await handle_update_family_context({"fact": "remember this"}, ctx)
            self.assertIn("updated", msg.lower())
            self.assertIn("remember this", (root / "context.md").read_text())

    async def test_person_cannot_be_override_filename(self):
        from tools.memory import handle_update_person_context

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ctx = SimpleNamespace(shadow=False, services=SimpleNamespace())
            with (
                patch("tools.memory._docs_root", return_value=root),
                patch("tools.memory._cfg", return_value={}),
                patch(
                    "constants.registry.resolve",
                    return_value="USER_OVERRIDE",
                ),
            ):
                msg = await handle_update_person_context(
                    {"person": "x", "fact": "nope"}, ctx
                )
            self.assertIn("Refused", msg)
            self.assertFalse((root / "USER_OVERRIDE.md").exists())


class TestOverrideInPrompt(unittest.TestCase):
    def test_build_system_prompt_includes_override(self):
        from context import build_system_prompt
        from datetime import timezone

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "USER_OVERRIDE.md").write_text("Immutable: pizza Fridays\n")
            cfg = {"timezone": "America/Halifax", "family_name": "Test"}
            with patch("context.DOCS_ROOT", str(root)):
                prompt = build_system_prompt(
                    cfg, timezone.utc, exclude_static=True
                )
            self.assertIn("USER_OVERRIDE", prompt)
            self.assertIn("pizza Fridays", prompt)


if __name__ == "__main__":
    unittest.main()
