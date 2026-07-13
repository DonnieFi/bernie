"""Model selection regression tests (stdlib unittest only)."""
from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestPoolFirstRouting(unittest.TestCase):
    def test_service_container_prefers_configured_litellm_pool_over_claude_prefix(self):
        from service_container import ServiceContainer

        container = ServiceContainer(
            anthropic=object(),
            litellm=object(),
            openrouter=object(),
            ollama="http://ollama",
        )
        cfg = {
            "anthropic_models": ["claude-sonnet-4-6"],
            "litellm_models": ["claude-proxy"],
            "ollama_models": [],
            "openrouter_direct": True,
        }

        with patch("config.config", cfg):
            self.assertIs(container.llm_for("claude-proxy"), container.openrouter)

    def test_service_container_litellm_pool_when_openrouter_direct_disabled(self):
        from service_container import ServiceContainer

        container = ServiceContainer(
            anthropic=object(),
            litellm=object(),
            openrouter=object(),
            ollama="http://ollama",
        )
        cfg = {
            "litellm_models": ["claude-proxy"],
            "openrouter_direct": False,
        }
        with patch("config.config", cfg):
            self.assertIs(container.llm_for("claude-proxy"), container.litellm)

    def test_service_container_prefers_configured_ollama_pool_over_prefixes(self):
        from service_container import ServiceContainer

        container = ServiceContainer(
            anthropic=object(),
            litellm=object(),
            ollama="http://ollama",
        )
        cfg = {
            "anthropic_models": ["claude-sonnet-4-6"],
            "litellm_models": [],
            "ollama_models": ["claude-local"],
        }

        with patch("config.config", cfg):
            self.assertEqual(container.llm_for("claude-local"), "http://ollama")


class TestDiscordModelTool(unittest.IsolatedAsyncioTestCase):
    async def test_reset_uses_configured_default_chat_model_before_legacy_default(self):
        from tools.admin import handle_litellm_switch_model

        ctx = SimpleNamespace(
            shadow=False,
            config={
                "default_chat_model": "or-default",
                "anthropic_models": ["claude-sonnet-4-6"],
                "litellm_models": ["or-default"],
                "ollama_models": [],
                "litellm_base_url": "https://litellm.example.local",
                "openrouter_direct": False,
            },
        )

        with (
            patch("llm.model_state.set_model") as mock_set_model,
            patch("config.update_config", new_callable=AsyncMock) as mock_update,
            patch("llm.clients.model_cache_support", return_value="cache note"),
        ):
            result = await handle_litellm_switch_model({"model_name": "reset"}, ctx)

        mock_set_model.assert_called_once_with("or-default", "https://litellm.example.local")
        mock_update.assert_awaited_once_with({"active_model": "or-default"})
        self.assertIn("or-default", result)


class TestLiteLLMModelSync(unittest.IsolatedAsyncioTestCase):
    async def test_sync_litellm_models_updates_config_from_db_names(self):
        from litellm_service import sync_config_litellm_models

        models = [
            {"model_name": "or-z"},
            {"model_name": "or-a"},
            {"model_name": "or-a"},
            {"model_info": {"id": "missing-name"}},
        ]

        with (
            patch("litellm_service.list_models", new_callable=AsyncMock, return_value=models),
            patch("config.update_config", new_callable=AsyncMock) as mock_update,
        ):
            synced = await sync_config_litellm_models()

        self.assertEqual(synced, ["or-a", "or-z"])
        mock_update.assert_awaited_once_with({"litellm_models": ["or-a", "or-z"]})

    async def test_sync_litellm_models_does_not_wipe_config_when_db_empty(self):
        from litellm_service import sync_config_litellm_models

        with (
            patch("litellm_service.list_models", new_callable=AsyncMock, return_value=[]),
            patch("config.update_config", new_callable=AsyncMock) as mock_update,
        ):
            synced = await sync_config_litellm_models()

        self.assertEqual(synced, [])
        mock_update.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
