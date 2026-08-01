"""Tests for Phase 4.4 Session 0 model config API surface (stdlib unittest only)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from api import create_api, verify_token
from service_container import ServiceContainer


class TestLLMModelConfig(unittest.TestCase):
    """Verify GET/PATCH for new model roles added in Session 0, and fallback enforcement."""

    @classmethod
    def setUpClass(cls):
        cls._web_tmp = tempfile.TemporaryDirectory()
        web_root = cls._web_tmp.name
        Path(web_root, "static").mkdir()
        Path(web_root, "index.html").write_text("<html></html>", encoding="utf-8")
        cls._web_patcher = patch("api.common.WEB_ROOT", web_root)
        cls._web_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls._web_patcher.stop()
        cls._web_tmp.cleanup()

    def setUp(self):
        self.container = ServiceContainer()
        self.app = create_api(None, self.container)
        self.client = TestClient(self.app)

        self._base_cfg = {
            "audit_model": "claude-haiku-4-5-20251001",
            "eval": {
                "eval_model": "claude-sonnet-4-6",
                "judge_fallback_model": "or-deepseek-v4",
                "judge_ollama_fallback": "hermes3:8b-llama3.1-q6_K",
            },
            "vision_model": "qwen3-vl:8b",
            "primary_reliable_model": "claude-sonnet-4-6",
            "cognitive_workers": {
                "research": {
                    "default_model": "qwen2.5:14b",
                    "upgrade_model": "claude-sonnet-4-6",
                },
                "study_guide": {"default_model": "hermes3:8b-llama3.1-q6_K"},
                "reflection": {"default_model": "hermes3:8b-llama3.1-q6_K"},
                "consolidation": {"default_model": "hermes3:8b-llama3.1-q6_K"},
            },
            "ollama_models": ["hermes3:8b-llama3.1-q6_K", "gemma:2b"],
            "litellm_models": ["or-deepseek-v4"],
            "anthropic_models": ["claude-sonnet-4-6"],
            "llm_fallback": {"model": "hermes3:8b-llama3.1-q6_K"},
            "active_model": "claude-sonnet-4-6",
        }
        self._config_patch = patch("api.common.config", self._base_cfg)
        self._config_patch.start()

        # Override auth
        def _fake_person():
            p = MagicMock()
            p.id = "person:test"
            p.role = "admin"
            p.name = "Test"
            return p
        self.app.dependency_overrides[verify_token] = _fake_person

    def tearDown(self):
        self._config_patch.stop()
        self.app.dependency_overrides.clear()

    def _get_models(self):
        with patch("api.common.get_model_info", return_value=("claude-sonnet-4-6", None)):
            resp = self.client.get("/api/config/models", headers={"Authorization": "Bearer test"})
            self.assertEqual(resp.status_code, 200, resp.text)
            return resp.json()

    def test_get_models_returns_every_new_model_role(self):
        data = self._get_models()
        for key in [
            "audit_model",
            "eval_model",
            "judge_fallback_model",
            "judge_ollama_fallback",
            "vision_model",
            "primary_reliable_model",
            "research_model",
            "research_upgrade_model",
            "study_guide_model",
            "reflection_model",
            "consolidation_model",
        ]:
            self.assertIn(key, data, f"missing {key} in GET /api/config/models response")
        self.assertEqual(data["research_upgrade_model"], "claude-sonnet-4-6")

    def test_get_models_merges_live_litellm_with_configured_aliases(self):
        """New aliases in config must not disappear when LiteLLM /v1/models responds."""

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def json(self):
                return {"data": [{"id": "or-live-only"}]}

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def get(self, *args, **kwargs):
                return _Resp()

        cfg = dict(self._base_cfg)
        cfg["litellm_models"] = ["or-config-only"]
        self.container.session = _Session()
        app = create_api(None, self.container)
        app.dependency_overrides[verify_token] = self.app.dependency_overrides[verify_token]
        client = TestClient(app)
        with (
            patch("api.common.config", cfg),
            patch("api.common.get_model_info", return_value=("or-config-only", "https://litellm.example.local")),
        ):
            resp = client.get("/api/config/models", headers={"Authorization": "Bearer test"})

        self.assertEqual(resp.status_code, 200, resp.text)
        litellm_ids = {m["id"] for m in resp.json()["models"] if m["source"] == "litellm"}
        self.assertIn("or-live-only", litellm_ids)
        self.assertIn("or-config-only", litellm_ids)

    def test_discord_target_routes_configured_claude_prefixed_litellm_alias_to_litellm(self):
        cfg = dict(self._base_cfg)
        cfg["litellm_models"] = ["claude-proxy"]
        cfg["anthropic_models"] = ["claude-sonnet-4-6"]
        cfg["litellm_base_url"] = "https://litellm.example.local"
        cfg["openrouter_direct"] = False
        with (
            patch("api.common.config", cfg),
            patch("llm.model_state.set_model") as mock_set_model,
            patch("config.update_config", new_callable=AsyncMock) as mock_update,
        ):
            resp = self.client.patch(
                "/api/config/models",
                json={"model": "claude-proxy", "target": "discord"},
                headers={"Authorization": "Bearer test"},
            )

        self.assertEqual(resp.status_code, 200, resp.text)
        mock_set_model.assert_called_once_with("claude-proxy", "https://litellm.example.local")
        mock_update.assert_awaited_once_with({"active_model": "claude-proxy"})

    def test_patch_roundtrips_audit_eval_judge_vision_primary_reliable(self):
        cases = [
            ("audit", "claude-sonnet-4-6", {"audit_model": "claude-sonnet-4-6"}),
            ("eval", "claude-sonnet-4-6", {"eval": {"eval_model": "claude-sonnet-4-6"}}),
            ("judge_fallback", "or-deepseek-v4", {"eval": {"judge_fallback_model": "or-deepseek-v4"}}),
            ("judge_ollama", "hermes3:8b-llama3.1-q6_K", {"eval": {"judge_ollama_fallback": "hermes3:8b-llama3.1-q6_K"}}),
            ("vision", "gemma:2b", {"vision_model": "gemma:2b"}),
            ("primary_reliable", "claude-sonnet-4-6", {"primary_reliable_model": "claude-sonnet-4-6"}),
            (
                "reflection",
                "hermes3:8b-llama3.1-q6_K",
                {"cognitive_workers": {"reflection": {"default_model": "hermes3:8b-llama3.1-q6_K"}}},
            ),
            (
                "consolidation",
                "hermes3:8b-llama3.1-q6_K",
                {"cognitive_workers": {"consolidation": {"default_model": "hermes3:8b-llama3.1-q6_K"}}},
            ),
            (
                "research_upgrade",
                "claude-sonnet-4-6",
                {"cognitive_workers": {"research": {"upgrade_model": "claude-sonnet-4-6"}}},
            ),
            (
                "study_guide",
                "hermes3:8b-llama3.1-q6_K",
                {"cognitive_workers": {"study_guide": {"default_model": "hermes3:8b-llama3.1-q6_K"}}},
            ),
        ]
        for target, model, expected_update in cases:
            with self.subTest(target=target), patch("config.update_config", new_callable=AsyncMock) as mock_update:
                resp = self.client.patch(
                    "/api/config/models",
                    json={"model": model, "target": target},
                    headers={"Authorization": "Bearer test"},
                )
                self.assertEqual(resp.status_code, 200, f"{target} failed: {resp.text}")
                mock_update.assert_awaited_once_with(expected_update)

    def test_model_role_pool_enforcement(self):
        invalid_cases = [
            ("judge_fallback", "hermes3:8b-llama3.1-q6_K", "litellm"),
            ("judge_fallback", "claude-sonnet-4-6", "litellm"),
            ("judge_ollama", "claude-sonnet-4-6", "ollama"),
            ("vision", "claude-sonnet-4-6", "ollama"),
            ("primary_reliable", "hermes3:8b-llama3.1-q6_K", "anthropic/litellm"),
        ]
        for target, model, expected_error in invalid_cases:
            with self.subTest(target=target, model=model):
                resp = self.client.patch(
                    "/api/config/models",
                    json={"model": model, "target": target},
                    headers={"Authorization": "Bearer test"},
                )
                self.assertEqual(resp.status_code, 400)
                self.assertIn(expected_error, resp.text.lower())

    def test_fallback_target_rejects_non_ollama(self):
        resp = self.client.patch(
            "/api/config/models",
            json={"model": "claude-sonnet-4-6", "target": "fallback"},
            headers={"Authorization": "Bearer test"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("ollama", resp.text.lower())

    def test_fallback_target_accepts_valid_ollama(self):
        with patch("config.update_config", new_callable=AsyncMock):
            resp = self.client.patch(
                "/api/config/models",
                json={"model": "hermes3:8b-llama3.1-q6_K", "target": "fallback"},
                headers={"Authorization": "Bearer test"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)


if __name__ == "__main__":
    unittest.main()
