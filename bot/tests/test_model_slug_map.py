"""Codex ↔ OpenRouter ↔ LiteLLM slug mapping."""

from __future__ import annotations

import unittest

from model_slug_map import (
    default_litellm_alias,
    openrouter_alias_table,
    resolve_canonical,
    resolve_litellm_alias,
    resolve_openrouter,
    validate_subscription_slug_alignment,
)


def _cfg() -> dict:
    return {
        "litellm_models": [
            "or-gpt54-mini",
            "or-gpt-56-sol",
            "or-gpt-5-4-mini",
        ],
        "subscription_models": [
            {
                "provider": "codex",
                "model": "gpt-5.4",
                "capabilities": ["text", "tools", "structured-output"],
                "openrouter_fallback_model": "openai/gpt-5.4",
                "litellm_alias": "or-gpt-5-4",
                "enabled": True,
            },
            {
                "provider": "codex",
                "model": "gpt-5.4-mini",
                "capabilities": ["text", "tools", "structured-output"],
                "openrouter_fallback_model": "openai/gpt-5.4-mini",
                "litellm_alias": "or-gpt54-mini",
                "enabled": True,
            },
            {
                "provider": "codex",
                "model": "gpt-5.6-sol",
                "capabilities": ["text", "tools", "structured-output"],
                "openrouter_fallback_model": "openai/gpt-5.6-sol",
                "litellm_alias": "or-gpt-56-sol",
                "enabled": True,
            },
        ],
    }


class TestModelSlugMap(unittest.TestCase):
    def test_default_litellm_alias_replaces_dots(self):
        self.assertEqual(default_litellm_alias("gpt-5.4"), "or-gpt-5-4")

    def test_resolve_openrouter_from_canonical_litellm_and_legacy(self):
        cfg = _cfg()
        self.assertEqual(resolve_openrouter("gpt-5.4", cfg), "openai/gpt-5.4")
        self.assertEqual(resolve_openrouter("or-gpt-5-4", cfg), "openai/gpt-5.4")
        self.assertEqual(resolve_openrouter("or-gpt-5-4-mini", cfg), "openai/gpt-5.4-mini")
        self.assertEqual(resolve_openrouter("or-gpt54-mini", cfg), "openai/gpt-5.4-mini")

    def test_resolve_canonical_from_any_slug(self):
        cfg = _cfg()
        self.assertEqual(resolve_canonical("or-gpt-56-sol", cfg), "gpt-5.6-sol")
        self.assertEqual(resolve_canonical("openai/gpt-5.4", cfg), "gpt-5.4")
        self.assertEqual(resolve_canonical("or-gpt-5-4-mini", cfg), "gpt-5.4-mini")

    def test_resolve_litellm_alias(self):
        cfg = _cfg()
        self.assertEqual(resolve_litellm_alias("gpt-5.4", cfg), "or-gpt-5-4")
        self.assertEqual(resolve_litellm_alias("openai/gpt-5.6-sol", cfg), "or-gpt-56-sol")

    def test_openrouter_alias_table_includes_legacy(self):
        table = openrouter_alias_table(_cfg())
        self.assertEqual(table["or-gpt-5-4-mini"], "openai/gpt-5.4-mini")
        self.assertEqual(table["gpt-5.4"], "openai/gpt-5.4")

    def test_validate_warns_on_unmapped_litellm_entry(self):
        cfg = _cfg()
        cfg["litellm_models"].append("or-unknown-model")
        warnings = validate_subscription_slug_alignment(cfg)
        self.assertTrue(any("or-unknown-model" in w for w in warnings))

    def test_subscription_model_resolves_litellm_alias(self):
        from completion_router import subscription_model

        entry = subscription_model("or-gpt-56-sol", _cfg())
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.model, "gpt-5.6-sol")


if __name__ == "__main__":
    unittest.main()
