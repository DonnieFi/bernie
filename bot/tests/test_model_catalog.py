"""Provider-aware catalog and selector helpers (family-bot-3an.5)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from model_catalog import (
    build_catalog,
    catalog_as_dicts,
    filter_for_active_auth,
    filter_for_target,
    format_chain,
    is_subscription_enabled,
    provider_readiness_map,
    validate_model_for_target,
)


def _cfg(**updates):
    cfg = {
        "anthropic_models": ["claude-sonnet"],
        "litellm_models": ["or-deepseek"],
        "ollama_models": ["qwen-local"],
        "llm_fallback": {"model": "qwen-local"},
        "openrouter_direct": True,
        "subscription_models": [
            {
                "provider": "grok",
                "model": "grok-4.5",
                "capabilities": ["text", "tools"],
                "openrouter_fallback_model": "x-ai/grok-4.5",
                "enabled": True,
            },
            {
                "provider": "codex",
                "model": "gpt-5.4",
                "capabilities": ["text", "tools", "structured-output"],
                "openrouter_fallback_model": "openai/gpt-5.4",
                "enabled": True,
            },
            {
                "provider": "grok",
                "model": "grok-text-only",
                "capabilities": ["text"],
                "openrouter_fallback_model": "x-ai/grok-4.5",
                "enabled": True,
            },
            {
                "provider": "grok",
                "model": "grok-tools-only",
                "capabilities": ["tools"],
                "openrouter_fallback_model": "x-ai/grok-4.5",
                "enabled": True,
            },
        ],
    }
    cfg.update(updates)
    return cfg


class TestModelCatalog(unittest.TestCase):
    def test_active_codex_auth_only_lists_direct_codex_models(self):
        entries = build_catalog(_cfg())
        visible = filter_for_active_auth(entries, "gpt-5.4")
        self.assertEqual({entry.provider for entry in visible}, {"codex"})
        self.assertEqual([entry.model for entry in visible], ["gpt-5.4"])

    def test_legacy_active_model_keeps_full_catalog(self):
        entries = build_catalog(_cfg())
        self.assertEqual(
            filter_for_active_auth(entries, "claude-sonnet"),
            entries,
        )

    def test_catalog_includes_providers_and_chains(self):
        entries = build_catalog(
            _cfg(),
            runner_health={"providers": {"codex": "ready", "grok": "reauth-required"}},
        )
        by_id = {e.model: e for e in entries}
        self.assertIn("claude-sonnet", by_id)
        self.assertIn("grok-4.5", by_id)
        self.assertEqual(by_id["grok-4.5"].provider, "grok")
        self.assertEqual(by_id["grok-4.5"].readiness, "reauth-required")
        chain = by_id["grok-4.5"].fallback_chain
        self.assertEqual(chain[0]["provider"], "grok")
        self.assertEqual(chain[1]["provider"], "openrouter")
        self.assertEqual(chain[2]["provider"], "ollama")

    def test_text_only_excluded_from_tools_and_structured_surfaces(self):
        entries = build_catalog(_cfg())
        tools_ids = {e.model for e in filter_for_target(entries, "active")}
        research_ids = {e.model for e in filter_for_target(entries, "research")}
        study_ids = {e.model for e in filter_for_target(entries, "study_guide")}
        self.assertNotIn("grok-text-only", tools_ids)
        self.assertNotIn("grok-text-only", research_ids)
        self.assertNotIn("grok-text-only", study_ids)
        self.assertIn("grok-4.5", tools_ids)
        self.assertIn("gpt-5.4", tools_ids)
        self.assertIn("gpt-5.4", research_ids)
        self.assertNotIn("grok-4.5", research_ids)
        self.assertNotIn("grok-tools-only", tools_ids)
        self.assertNotIn("grok-tools-only", research_ids)
        self.assertIn("qwen-local", study_ids)  # ollama structured-output
        # text-only ok for digest (text only)
        digest_ids = {e.model for e in filter_for_target(entries, "digest")}
        self.assertIn("grok-text-only", digest_ids)

    def test_vision_target_ollama_only(self):
        cfg = _cfg(vision_model="qwen-local")
        entries = filter_for_target(build_catalog(cfg), "vision")
        ids = {e.model for e in entries}
        # Any configured Ollama id is selectable for vision (switchable pool).
        self.assertIn("qwen-local", ids)
        self.assertNotIn("claude-sonnet", ids)
        self.assertNotIn("grok-4.5", ids)
        # Still switchable when current vision_model is a different ollama id.
        cfg2 = _cfg(vision_model="other-vl", ollama_models=["qwen-local", "other-vl"])
        entries2 = filter_for_target(build_catalog(cfg2), "vision")
        self.assertEqual({e.model for e in entries2}, {"qwen-local", "other-vl"})

    def test_primary_reliable_excludes_subscription(self):
        entries = filter_for_target(build_catalog(_cfg()), "primary_reliable")
        ids = {e.model for e in entries}
        self.assertIn("claude-sonnet", ids)
        self.assertNotIn("grok-4.5", ids)
        self.assertNotIn("gpt-5.4", ids)

    def test_fallback_target_ollama_only(self):
        err = validate_model_for_target("claude-sonnet", "fallback", _cfg())
        self.assertIsNotNone(err)
        self.assertIn("ollama", err.lower())
        self.assertIsNone(validate_model_for_target("qwen-local", "fallback", _cfg()))

    def test_judge_fallback_litellm_only(self):
        err = validate_model_for_target("qwen-local", "judge_fallback", _cfg())
        self.assertIsNotNone(err)
        self.assertIn("litellm", err.lower())

    def test_unknown_model_rejected(self):
        err = validate_model_for_target("no-such-model", "digest", _cfg())
        self.assertIn("unknown", err.lower())

    def test_format_chain_and_subscription_flag(self):
        entries = build_catalog(_cfg())
        grok = next(e for e in entries if e.model == "grok-4.5")
        text = format_chain(grok, "grok-4.5", _cfg())
        self.assertIn("grok/grok-4.5", text)
        self.assertTrue(is_subscription_enabled("grok-4.5", _cfg()))

    def test_live_litellm_merge(self):
        entries = build_catalog(_cfg(), extra_litellm_ids=["or-live-only"])
        ids = {e.model for e in entries}
        self.assertIn("or-live-only", ids)
        self.assertIn("or-deepseek", ids)

    def test_ollama_readiness_rejects_placeholder_urls(self):
        readiness = provider_readiness_map({
            "ollama_models": ["qwen"],
            "ollama_base_urls": ["http://192.168.1.X:11434"],
        })
        self.assertEqual(readiness["ollama"], "unavailable")
        readiness_ok = provider_readiness_map({
            "ollama_models": ["qwen"],
            "ollama_base_url": "http://192.168.1.X:11434",
        })
        self.assertEqual(readiness_ok["ollama"], "ready")

    def test_dicts_for_api(self):
        rows = catalog_as_dicts(_cfg(), target="active")
        self.assertTrue(any(r["provider"] == "grok" for r in rows))
        self.assertFalse(any(r["model"] == "grok-text-only" for r in rows))


if __name__ == "__main__":
    unittest.main()
