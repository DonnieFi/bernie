"""Provider-neutral subscription routing contract tests."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from completion_router import (
    CompletionError,
    CompletionErrorCode,
    CompletionRequest,
    CompletionResult,
    ProviderReadiness,
    ToolCall,
    resolve_fallback_chain,
    subscription_catalog,
)
from config_validate import validate_config_core
from model_registry import model_source
from service_container import ServiceContainer


def _core(**updates):
    cfg = {
        "timezone": "America/Halifax",
        "schedule_channel_id": 1,
        "guild_id": 2,
        "poll_interval_minutes": 5,
        "family_members": {},
        "anthropic_models": ["claude-sonnet"],
        "litellm_models": ["or-legacy"],
        "ollama_models": ["qwen-local", "surface-local"],
        "llm_fallback": {"model": "qwen-local"},
        "subscription_models": [{
            "provider": "codex",
            "model": "gpt-codex",
            "capabilities": ["text", "tools", "structured-output"],
            "openrouter_fallback_model": "openai/gpt-codex",
            "enabled": True,
        }],
    }
    cfg.update(updates)
    return cfg


class TestSubscriptionRouting(unittest.TestCase):
    def test_catalog_and_contract_share_provider_tool_and_error_shapes(self):
        entry = subscription_catalog(_core())[0]
        self.assertEqual(entry.provider, "codex")
        self.assertEqual(entry.readiness, ProviderReadiness.UNKNOWN)
        self.assertIn("tools", entry.capabilities)

        request = CompletionRequest(
            surface="research",
            provider="codex",
            model=entry.model,
            tools=({"name": "search"},),
            output_schema={"type": "object"},
        )
        result = CompletionResult(
            provider="codex",
            model=entry.model,
            tool_calls=(ToolCall("1", "search", {"q": "test"}),),
            error=CompletionError(CompletionErrorCode.BUSY, retryable=True),
        )
        self.assertEqual(request.tools[0]["name"], result.tool_calls[0].name)
        self.assertTrue(result.error.retryable)

    def test_fallback_chain_uses_surface_then_global_ollama_selection(self):
        cfg = _core()
        surface_chain = resolve_fallback_chain(
            "gpt-codex", cfg, surface_ollama_model="surface-local"
        )
        global_chain = resolve_fallback_chain("gpt-codex", cfg)

        self.assertEqual(
            [(target.provider, target.model) for target in surface_chain],
            [
                ("codex", "gpt-codex"),
                ("openrouter", "openai/gpt-codex"),
                ("ollama", "surface-local"),
            ],
        )
        self.assertEqual(global_chain[-1].model, "qwen-local")

    def test_invalid_subscription_configuration_fails_closed(self):
        cfg = _core()
        cfg["subscription_models"][0]["openrouter_fallback_model"] = ""
        with self.assertRaisesRegex(ValueError, "openrouter_fallback_model"):
            validate_config_core(cfg)

        cfg = _core()
        cfg["anthropic_models"].append("gpt-codex")
        with self.assertRaisesRegex(ValueError, "overlap legacy model pools"):
            validate_config_core(cfg)

    def test_fallback_chain_openrouter_only_when_no_ollama(self):
        cfg = _core()
        cfg["ollama_models"] = []
        cfg["llm_fallback"] = {}
        chain = resolve_fallback_chain("gpt-codex", cfg)
        self.assertEqual(
            [(t.provider, t.model) for t in chain],
            [("codex", "gpt-codex"), ("openrouter", "openai/gpt-codex")],
        )

    def test_fallback_chain_requires_at_least_one_fallback_tier(self):
        cfg = _core()
        cfg["subscription_models"][0]["openrouter_fallback_model"] = ""
        cfg["ollama_models"] = []
        with self.assertRaisesRegex(ValueError, "openrouter_fallback_model"):
            resolve_fallback_chain("gpt-codex", cfg)

    def test_registry_ignores_disabled_subscription(self):
        cfg = _core()
        cfg["subscription_models"][0]["enabled"] = False
        self.assertNotEqual(model_source("gpt-codex", cfg), "codex")
        cfg = _core()
        cfg["subscription_models"].append({
            "provider": "grok",
            "model": "grok-build",
            "capabilities": ["text"],
            "openrouter_fallback_model": "x-ai/grok-build",
            "enabled": True,
        })
        self.assertEqual(model_source("gpt-codex", cfg), "codex")
        self.assertEqual(model_source("grok-build", cfg), "grok")
        self.assertEqual(model_source("claude-sonnet", cfg), "anthropic")

        with patch("config.config", cfg):
            with self.assertRaisesRegex(ValueError, "CompletionRouter"):
                ServiceContainer().llm_for("gpt-codex")


if __name__ == "__main__":
    unittest.main()
