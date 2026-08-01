"""family-bot julyaudit.html P0/P1 fixes + review follow-ups."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _root() -> Path:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "web" / "static" / "js" / "app.v6.js").exists():
            return p
    return here.parents[2]


class TestPresenceEnrichCooldown(unittest.TestCase):
    def test_cooldown_var_consistent(self):
        app = (_root() / "web" / "static" / "js" / "app.v6.js").read_text(encoding="utf-8")
        self.assertIn("_presenceEnrichCooldown", app)
        self.assertNotIn("_presenceEnrichAt", app)
        self.assertIn("_presenceEnrichCooldown = Date.now()", app)


class TestTodayTasksSoftFetch(unittest.TestCase):
    def test_refresh_today_fetches_tasks(self):
        app = (_root() / "web" / "static" / "js" / "app.v6.js").read_text(encoding="utf-8")
        self.assertIn("/api/tasks?all_people=true", app)
        self.assertIn("family-bot-julyaudit P0", app)
        today = (_root() / "web" / "static" / "js" / "v3_today.js").read_text(encoding="utf-8")
        self.assertIn("collectAttentionItems", today)
        self.assertIn("D.tasks", today)

    def test_today_api_embeds_tasks(self):
        candidates = [
            _root() / "bot" / "api" / "routes" / "today.py",
            _root() / "api" / "routes" / "today.py",
            Path(__file__).resolve().parents[1] / "api" / "routes" / "today.py",
        ]
        p = next((c for c in candidates if c.exists()), None)
        self.assertIsNotNone(p)
        src = p.read_text(encoding="utf-8")
        self.assertIn("attention_tasks", src)
        self.assertIn('"tasks": attention_tasks', src)

    def test_current_user_no_dad_default(self):
        today = (_root() / "web" / "static" / "js" / "v3_today.js").read_text(encoding="utf-8")
        self.assertIn("window.Me && (window.Me.id || window.Me.name)", today)
        self.assertNotIn('|| "dad"', today)
        # empty-string fallback, not a hardcoded family member
        self.assertIn('|| ""', today)


class TestSetPresenceAuthz(unittest.TestCase):
    def test_route_source_has_authz(self):
        candidates = [
            _root() / "bot" / "api" / "routes" / "home.py",
            _root() / "api" / "routes" / "home.py",
            Path(__file__).resolve().parents[1] / "api" / "routes" / "home.py",
        ]
        p = next((c for c in candidates if c.exists()), None)
        self.assertIsNotNone(p)
        src = p.read_text(encoding="utf-8")
        self.assertIn("Can only set your own presence", src)
        self.assertIn("role != \"admin\"", src)


class TestSetPresenceAuthzHTTP(unittest.TestCase):
    """Behavioral FastAPI: family cannot spoof another member; admin can."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from api import Person, create_api, verify_token

        self.Person = Person
        self.verify_token = verify_token
        container = MagicMock()
        container.db = MagicMock()
        container.task_store = MagicMock()
        container.unified_tasks = MagicMock()
        container.bot = None
        container.session = None
        container.http_session = None
        container.frigate = MagicMock()
        container.notification_orchestrator = None
        container.notification_dispatcher = None
        container.calendar = MagicMock()
        container.calendar_service = MagicMock()
        container.weather = None
        container.weather_module = None
        container.ha = MagicMock()
        container.ha_service = MagicMock()
        container.summary_builder = None
        container.connection_manager = MagicMock()
        container.supervisor = None
        self.app = create_api(None, container)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def _as(self, person_id: str, role: str):
        # Person only has id + role (api.common.Person)
        self.app.dependency_overrides[self.verify_token] = lambda: self.Person(
            id=person_id, role=role,
        )

    def test_family_cannot_set_other_person(self):
        self._as("child1", "kids")
        with patch("db_writes.update_presence", new_callable=AsyncMock) as mock_up:
            resp = self.client.post(
                "/api/presence/dad/set",
                json={"home": True},
                headers={"X-Bernie-Token": "t"},
            )
        self.assertEqual(resp.status_code, 403, resp.text)
        mock_up.assert_not_awaited()

    def test_admin_can_set_other_person(self):
        self._as("dad", "admin")
        with patch("db_writes.update_presence", new_callable=AsyncMock) as mock_up:
            resp = self.client.post(
                "/api/presence/child1/set",
                json={"home": False},
                headers={"X-Bernie-Token": "t"},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        mock_up.assert_awaited()
        body = resp.json()
        self.assertEqual(body.get("ok"), True)
        self.assertEqual(body.get("home"), False)

    def test_self_can_set_own_presence(self):
        self._as("child1", "kids")
        with patch("db_writes.update_presence", new_callable=AsyncMock) as mock_up:
            resp = self.client.post(
                "/api/presence/child1/set",
                json={"home": True},
                headers={"X-Bernie-Token": "t"},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        mock_up.assert_awaited()


class TestPresenceSnapshotRoutes(unittest.TestCase):
    def test_me_and_presence_use_for_web(self):
        for rel in (
            ("api", "routes", "auth.py"),
            ("api", "routes", "home.py"),
        ):
            candidates = [
                _root() / "bot" / Path(*rel),
                _root() / Path(*rel),
                Path(__file__).resolve().parents[1] / Path(*rel),
            ]
            p = next((c for c in candidates if c.exists()), None)
            self.assertIsNotNone(p, rel)
            src = p.read_text(encoding="utf-8")
            self.assertIn("get_full_presence_for_web", src)


class TestCopyDoorQa(unittest.TestCase):
    def test_qa_not_cameras_label(self):
        today = (_root() / "web" / "static" / "js" / "v3_today.js").read_text(encoding="utf-8")
        self.assertIn('"Door"', today)
        self.assertNotIn('"Cameras"', today)


if __name__ == "__main__":
    unittest.main()
