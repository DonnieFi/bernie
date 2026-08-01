"""family-bot-9vr: tool surface shrink & dead-code deletes (cheap wins)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _root() -> Path:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "web" / "static" / "css" / "panels.css").exists():
            return p
        if (p / "bot" / "llm" / "chat.py").exists() or (p / "llm" / "chat.py").exists():
            # prefer repo root that also has web/
            if (p / "web").exists():
                return p
            if (p.parent / "web").exists():
                return p.parent
            return p
    return here.parents[2]


class Test4hbMealDeleted(unittest.TestCase):
    def test_no_chat_meal_planning(self):
        import importlib
        chat_mod = importlib.import_module("llm.chat")
        self.assertFalse(hasattr(chat_mod, "chat_meal_planning"))
        text = Path(chat_mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn("async def chat_meal_planning", text)


class Test19lModelPreferenceGone(unittest.TestCase):
    def test_modes_no_model_preference(self):
        from modes import ModeDefinition, load_all_modes
        load_all_modes.cache_clear = getattr(load_all_modes, "cache_clear", lambda: None)
        # clear internal cache
        import modes as modes_mod
        modes_mod._modes = {}
        modes = load_all_modes()
        self.assertTrue(modes)
        self.assertFalse(hasattr(ModeDefinition, "__dataclass_fields__") and
                         "model_preference" in ModeDefinition.__dataclass_fields__)
        for slug, m in modes.items():
            self.assertFalse(hasattr(m, "model_preference") and getattr(m, "model_preference", None),
                             f"{slug} still has model_preference")
            # field removed
            self.assertNotIn("model_preference", m.__dataclass_fields__)


class TestQvmSlashAliases(unittest.TestCase):
    def test_today_addevent_removed(self):
        candidates = [
            _root() / "bot" / "slash" / "family_cmds.py",
            _root() / "slash" / "family_cmds.py",
            Path(__file__).resolve().parents[1] / "slash" / "family_cmds.py",
        ]
        p = next((c for c in candidates if c.exists()), None)
        if p is None:
            self.skipTest("family_cmds.py not found in this layout")
        src = p.read_text(encoding="utf-8")
        self.assertIn("family-bot-qvm", src)
        self.assertNotIn('@tree.command(name="today"', src)
        self.assertNotIn('@tree.command(name="addevent"', src)
        self.assertNotIn("cmd_today", src)
        self.assertNotIn("cmd_addevent", src)


class TestLyeLightTileGone(unittest.TestCase):
    def test_panels_css(self):
        p = _root() / "web" / "static" / "css" / "panels.css"
        src = p.read_text(encoding="utf-8")
        self.assertIn("family-bot-lye", src)
        self.assertNotIn(".light-tile {", src)


class Test1a8WebhookGated(unittest.TestCase):
    def test_webhook_requires_auth(self):
        candidates = [
            _root() / "bot" / "api" / "routes" / "home.py",
            _root() / "api" / "routes" / "home.py",
            Path(__file__).resolve().parents[1] / "api" / "routes" / "home.py",
        ]
        p = next((c for c in candidates if c.exists()), None)
        if p is None:
            self.skipTest("home.py not found")
        src = p.read_text(encoding="utf-8")
        self.assertIn("family-bot-1a8", src)
        self.assertIn("410", src)

    def test_debug_config_admin_only(self):
        candidates = [
            _root() / "bot" / "api" / "routes" / "home.py",
            _root() / "api" / "routes" / "home.py",
            Path(__file__).resolve().parents[1] / "api" / "routes" / "home.py",
        ]
        p = next((c for c in candidates if c.exists()), None)
        if p is None:
            self.skipTest("home.py not found")
        src = p.read_text(encoding="utf-8")
        self.assertIn("/api/debug/config", src)
        self.assertIn("require_admin", src)


class TestDy7RootCruftNotAtRoot(unittest.TestCase):
    def test_patches_not_at_repo_root(self):
        root = _root()
        # Giant historical patches must not live at repo root
        for name in (
            "diff_arch.patch",
            "diff_db.patch",
            "diff_slash.patch",
            "diff_tools.patch",
            "last_5_commits.patch",
        ):
            self.assertFalse((root / name).exists(), f"{name} still at repo root")
        # Prefer no multi-MB patch blobs tracked under archive either
        cruft = root / "docs" / "archive" / "root-cruft"
        if cruft.is_dir():
            patches = list(cruft.glob("*.patch"))
            self.assertEqual(patches, [], f"stale patches still in archive: {patches}")


if __name__ == "__main__":
    unittest.main()
