"""Wave-1 review feedback regressions (HA routes, CORS, intents, identity).

Keeps deps light so the module runs without anthropic/discord installed:
intent helpers load via importlib; API identity is a source contract.
Full runtime suite runs on bernie-host with project venv.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOT = Path(__file__).resolve().parents[1]


def _load_pure(mod_name: str, path: Path):
    """Load a module file without executing package __init__ side effects."""
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestHaAutomationsUi(unittest.TestCase):
    def test_v3_home_uses_ha_toggle_not_bernie_delete(self):
        src = (ROOT / "web/static/js/v3_home.js").read_text(encoding="utf-8")
        self.assertIn("/api/ha/automations/", src)
        self.assertIn("/toggle", src)
        # Del for HA rows wrongly hit Bernie CRUD — must stay removed
        self.assertNotRegex(
            src,
            r'api\(`/api/automations/\$\{a\.id\}`,\s*\{\s*method:\s*"DELETE"',
        )

    def test_api_registers_ha_paths(self):
        src = "\n".join(p.read_text(encoding="utf-8") for p in sorted((BOT / "api").rglob("*.py")))
        self.assertIn('"/api/ha/automations"', src)
        self.assertIn('"/api/ha/automations/{id}/toggle"', src)
        self.assertIn('"/api/automations"', src)  # Bernie CRUD remains separate

    def test_light_api_broadcasts_light_state_shape(self):
        """Client WS listens for light.state + id (ha_service shape), not light.update."""
        src = (BOT / "api/routes/home.py").read_text(encoding="utf-8")
        self.assertIn('"type": "light.state"', src)
        self.assertIn("_light_state_event", src)
        self.assertIn("_invalidate_home_caches", src)
        self.assertNotIn('"type": "light.update"', src)

    def test_home_ui_error_and_refresh_hooks(self):
        home = (ROOT / "web/static/js/v3_home.js").read_text(encoding="utf-8")
        app = (ROOT / "web/static/js/app.v6.js").read_text(encoding="utf-8")
        self.assertIn("_homeLoadError", home)
        self.assertIn("Home Assistant unreachable", home)
        self.assertIn("window.refreshHomeData", app)
        self.assertIn("light.update", app)  # legacy tolerance


class TestCorsPolicy(unittest.TestCase):
    def test_wildcard_refused_helper(self):
        from config_validate import cors_origins_refused, validate_config

        self.assertTrue(cors_origins_refused({"cors_origins": "*"}))
        self.assertTrue(cors_origins_refused({"cors_origins": ["*"]}))
        self.assertFalse(cors_origins_refused({"cors_origins": []}))
        self.assertFalse(cors_origins_refused({}))

        findings = validate_config({"cors_origins": "*"})
        codes = {f["code"] for f in findings}
        self.assertIn("cors_wildcard", codes)

    def test_api_create_refuses_star(self):
        """Source contract: '*' → empty allowlist (not open CORS)."""
        src = "\n".join(p.read_text(encoding="utf-8") for p in sorted((BOT / "api").rglob("*.py")))
        self.assertIn('cors_origins == ["*"]', src)
        self.assertIn("refusing open CORS", src)
        self.assertRegex(src, r"cors_origins\s*=\s*\[\]")


class TestMasterTokenIdentity(unittest.TestCase):
    def test_master_token_maps_to_api_service(self):
        src = "\n".join(p.read_text(encoding="utf-8") for p in sorted((BOT / "api").rglob("*.py")))
        self.assertIn('Person(id="api_service", role="admin")', src)
        self.assertIn("service principal", src.lower())

    def test_require_admin_present(self):
        """require_admin is intentionally narrow (restart), not broad JWT gating."""
        src = "\n".join(p.read_text(encoding="utf-8") for p in sorted((BOT / "api").rglob("*.py")))
        self.assertIn("async def require_admin", src)
        deps = re.findall(r"Depends\((?:_ac\.)?require_admin\)", src)
        self.assertGreaterEqual(len(deps), 1)


class TestHomeAndScheduleIntent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legs = _load_pure(
            "context_legs_review_test",
            BOT / "llm" / "context_legs.py",
        )

    def test_looks_home_intent_not_bare_home_room(self):
        looks_home_intent = self.legs.looks_home_intent
        self.assertFalse(looks_home_intent("heading home soon"))
        self.assertFalse(looks_home_intent("my room is messy"))
        self.assertTrue(looks_home_intent("turn on the kitchen light"))
        self.assertTrue(looks_home_intent("who's home?"))
        self.assertTrue(looks_home_intent("smart home status"))

    def test_looks_schedule_sleepover_still_matches(self):
        looks_schedule_intent = self.legs.looks_schedule_intent
        self.assertTrue(looks_schedule_intent("help me plan a sleepover"))
        self.assertTrue(looks_schedule_intent("any homework tonight?"))
        # bare plan without schedule noun — should not force calendar
        self.assertFalse(looks_schedule_intent("help me plan dinner"))


class TestStudyKeywordDefaults(unittest.TestCase):
    def test_defaults_agree(self):
        nudge = (BOT / "proactive_nudge.py").read_text(encoding="utf-8")
        study = (BOT / "cognitive_workers" / "study_detection.py").read_text(encoding="utf-8")
        expected = (
            r"test|exam|quiz|rehearsal|recital|audition|midterm|finals?|final exam"
        )
        self.assertIn(expected, nudge)
        self.assertIn(expected, study)
        self.assertNotIn("project|presentation", expected)
        # old noisy defaults must not reappear in defaults
        self.assertNotIn(
            "midterm|final|project|presentation",
            study,
        )


class TestLogsHttpSnapshot(unittest.TestCase):
    def test_no_ws_logs_endpoint(self):
        src = "\n".join(p.read_text(encoding="utf-8") for p in sorted((BOT / "api").rglob("*.py")))
        self.assertNotIn('@app.websocket("/ws/logs")', src)
        self.assertIn('"/api/logs"', src)

    def test_ui_uses_http_not_ws(self):
        js = (ROOT / "web/static/js/v3_logs.js").read_text(encoding="utf-8")
        self.assertIn("/api/logs", js)
        self.assertNotIn("WebSocket", js)
        self.assertNotIn("ws/logs", js)


class TestBackupPathContract(unittest.TestCase):
    def test_backup_default_dir_and_name(self):
        # database package (8lx monofile split) — backup lives in misc.py
        src = (BOT / "database" / "misc.py").read_text(encoding="utf-8")
        self.assertIn("/data/backups", src)
        self.assertIn("family_bot-", src)
        self.assertIn("VACUUM INTO", src)


class TestConfigPersonalityAllowlist(unittest.TestCase):
    def test_personality_allowlist_includes_soul_people_not_deploy(self):
        from personality_files import is_editable_personality_rel

        self.assertTrue(is_editable_personality_rel("soul.md"))
        self.assertTrue(is_editable_personality_rel("bernie.md"))
        self.assertTrue(is_editable_personality_rel("dad.md"))
        self.assertTrue(is_editable_personality_rel("family/dad.md"))
        self.assertFalse(is_editable_personality_rel("deploy.md"))
        self.assertFalse(is_editable_personality_rel("adr/0001.md"))
        self.assertFalse(is_editable_personality_rel("archive/foo.md"))
        self.assertFalse(is_editable_personality_rel("../config.json"))
        self.assertFalse(is_editable_personality_rel("config.json"))

    def test_list_prefers_core_then_people(self):
        from personality_files import list_editable_personality_files
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "soul.md").write_text("s", encoding="utf-8")
            (root / "bernie.md").write_text("b", encoding="utf-8")
            (root / "dad.md").write_text("d", encoding="utf-8")
            (root / "deploy.md").write_text("nope", encoding="utf-8")
            (root / "family").mkdir()
            (root / "family" / "mom.md").write_text("m", encoding="utf-8")
            (root / "family" / "README.md").write_text("skip", encoding="utf-8")
            files = list_editable_personality_files(root)
            self.assertIn("soul.md", files)
            self.assertIn("bernie.md", files)
            self.assertIn("dad.md", files)
            self.assertIn("family/mom.md", files)
            self.assertNotIn("deploy.md", files)
            self.assertNotIn("family/README.md", files)


if __name__ == "__main__":
    unittest.main()
