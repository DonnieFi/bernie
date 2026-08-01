"""Startup route validation without Anthropic (family-bot-3an.6).

Tests call production ``has_usable_llm_route`` / ``MissingProviderClient`` —
no mirrored helper that only copies main.py.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from completion_router import (
    MissingProviderClient,
    has_usable_llm_route,
    subscription_route_viable,
    subscription_catalog,
)


def _sub_entry(**overrides):
    base = {
        "provider": "grok",
        "model": "grok-4.5",
        "enabled": True,
        "openrouter_fallback_model": "x-ai/grok-4.5",
        "capabilities": ["text", "tools"],
    }
    base.update(overrides)
    return base


class TestHasUsableLlmRoute(unittest.TestCase):
    def test_legacy_anthropic_ready(self):
        ok, reason = has_usable_llm_route({}, env={"ANTHROPIC_API_KEY": "sk-test"})
        self.assertTrue(ok)
        self.assertEqual(reason, "anthropic")

    def test_direct_openrouter_without_anthropic(self):
        ok, reason = has_usable_llm_route(
            {"subscription_models": [], "ollama_models": []},
            env={"OPENROUTER_API_KEY": "or-key"},
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "openrouter")

    def test_ollama_only_startup(self):
        ok, reason = has_usable_llm_route(
            {
                "ollama_models": ["qwen-local"],
                "ollama_base_url": "http://192.168.1.X:11434",
                "subscription_models": [],
            },
            env={},
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ollama")

    def test_placeholder_ollama_not_viable(self):
        ok, reason = has_usable_llm_route(
            {
                "ollama_models": ["qwen-local"],
                "ollama_base_url": "http://192.168.1.X:11434",
                "subscription_models": [],
            },
            env={},
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "none")

    def test_enabled_subscription_alone_not_usable(self):
        """Enabled entry without OpenRouter key and without Ollama fallback fails."""
        cfg = {
            "subscription_models": [_sub_entry()],
            "ollama_models": [],
            "llm_fallback": {},
        }
        ok, reason = has_usable_llm_route(cfg, env={})
        self.assertFalse(ok)
        self.assertEqual(reason, "none")

    def test_subscription_with_ollama_fallback_viable(self):
        cfg = {
            "subscription_models": [_sub_entry()],
            "ollama_models": ["qwen-local"],
            "ollama_base_url": "http://192.168.1.X:11434",
            "llm_fallback": {"model": "qwen-local"},
        }
        ok, reason = has_usable_llm_route(cfg, env={})
        self.assertTrue(ok)
        self.assertTrue(reason.startswith("subscription:grok:"))

    def test_subscription_with_openrouter_key_viable(self):
        cfg = {
            "subscription_models": [_sub_entry()],
            "ollama_models": [],
        }
        ok, reason = has_usable_llm_route(
            cfg, env={"OPENROUTER_API_KEY_LITE": "lite-key"},
        )
        # OpenRouter key alone already satisfies the gate before subscription.
        self.assertTrue(ok)
        self.assertEqual(reason, "openrouter")

    def test_disabled_subscription_ignored(self):
        cfg = {
            "subscription_models": [_sub_entry(enabled=False)],
            "ollama_models": [],
        }
        ok, reason = has_usable_llm_route(cfg, env={})
        self.assertFalse(ok)
        self.assertEqual(reason, "none")

    def test_missing_openrouter_mapping_not_viable(self):
        cfg = {
            "subscription_models": [_sub_entry(openrouter_fallback_model="")],
            "ollama_models": ["qwen"],
            "ollama_base_url": "http://192.168.1.X:11434",
            "llm_fallback": {"model": "qwen"},
        }
        # empty mapping raises at catalog parse — gate must not crash
        ok, reason = has_usable_llm_route(cfg, env={})
        # ollama still saves startup
        self.assertTrue(ok)
        self.assertEqual(reason, "ollama")

    def test_reauth_required_still_starts_with_fallback(self):
        cfg = {
            "subscription_models": [_sub_entry()],
            "ollama_models": ["qwen"],
            "ollama_base_url": "http://192.168.1.X:11434",
            "llm_fallback": {"model": "qwen"},
        }
        ok, reason = has_usable_llm_route(
            cfg,
            env={},
            runner_health={"providers": {"grok": "reauth-required", "codex": "unavailable"}},
        )
        self.assertTrue(ok)
        self.assertIn("reauth-required", reason)

    def test_unavailable_runner_still_starts_with_fallback(self):
        cfg = {
            "subscription_models": [_sub_entry(provider="codex", model="gpt-5.4")],
            "ollama_models": ["qwen"],
            "ollama_base_url": "http://192.168.1.X:11434",
            "llm_fallback": {"model": "qwen"},
        }
        ok, reason = has_usable_llm_route(
            cfg,
            env={},
            runner_health={"providers": {"codex": "unavailable"}},
        )
        self.assertTrue(ok)
        self.assertIn("unavailable", reason)

    def test_none_when_empty(self):
        ok, reason = has_usable_llm_route(
            {"subscription_models": [], "ollama_models": []},
            env={},
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "none")

    def test_subscription_route_viable_helper(self):
        cfg = {
            "subscription_models": [_sub_entry()],
            "ollama_models": ["qwen"],
            "ollama_base_url": "http://192.168.1.X:11434",
            "llm_fallback": {"model": "qwen"},
        }
        entry = subscription_catalog(cfg)[0]
        self.assertTrue(subscription_route_viable(entry, cfg, env={}))
        self.assertTrue(
            subscription_route_viable(
                entry, {"ollama_models": []}, env={"OPENROUTER_API_KEY": "or-key"},
            )
        )
        self.assertFalse(
            subscription_route_viable(entry, {"ollama_models": []}, env={})
        )
        self.assertFalse(subscription_route_viable(
            entry,
            {"ollama_models": ["qwen"], "llm_fallback": {}},
            env={},
        ))


class TestMissingProviderClient(unittest.TestCase):
    def test_llm_for_raises_typed_retryable_when_anthropic_missing(self):
        from service_container import ServiceContainer

        c = ServiceContainer(
            anthropic=None,
            litellm=None,
            openrouter=None,
            ollama="http://ollama",
        )
        cfg = {
            "anthropic_models": ["claude-sonnet-4-6"],
            "litellm_models": [],
            "ollama_models": [],
        }
        with patch("config.config", cfg):
            with self.assertRaises(MissingProviderClient) as ctx:
                c.llm_for("claude-sonnet-4-6")
        self.assertTrue(ctx.exception.retryable)
        err = ctx.exception.as_completion_error()
        self.assertTrue(err.retryable)
        self.assertEqual(err.code.value, "unavailable")

    def test_llm_for_raises_for_missing_litellm(self):
        from service_container import ServiceContainer

        c = ServiceContainer(anthropic=None, litellm=None, openrouter=None, ollama="http://o")
        cfg = {
            "anthropic_models": [],
            "litellm_models": ["or-deepseek"],
            "ollama_models": [],
            "openrouter_direct": False,
        }
        with patch("config.config", cfg):
            with self.assertRaises(MissingProviderClient) as ctx:
                c.llm_for("or-deepseek")
        self.assertTrue(ctx.exception.retryable)

    def test_health_payload_has_no_identity_fields(self):
        from model_catalog import provider_readiness_map
        readiness = provider_readiness_map(
            {"ollama_models": ["x"]},
            runner_health={
                "providers": {"codex": "ready", "grok": "reauth-required"},
                "email": "should-not-appear",
                "token": "secret",
            },
        )
        self.assertEqual(set(readiness.keys()), {
            "anthropic", "openrouter", "litellm", "ollama", "codex", "grok",
        })
        self.assertNotIn("email", readiness)
        self.assertNotIn("token", readiness)


if __name__ == "__main__":
    unittest.main()
