"""Placeholder Ollama URL detection (family-bot-gb64)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ollama_resolver import (
    configured_ollama_base_url,
    is_placeholder_ollama_url,
)


class TestOllamaPlaceholder(unittest.TestCase):
    def test_detects_placeholder(self):
        self.assertTrue(is_placeholder_ollama_url("http://192.168.1.X:11434"))
        self.assertTrue(is_placeholder_ollama_url(None))
        self.assertFalse(is_placeholder_ollama_url("http://192.168.1.X:11434"))

    def test_configured_returns_none_for_placeholder(self):
        cfg = {"ollama_base_url": "http://192.168.1.X:11434", "ollama_models": ["qwen"]}
        self.assertIsNone(configured_ollama_base_url(cfg))

    def test_configured_returns_url_when_set(self):
        cfg = {"ollama_base_url": "http://192.168.1.X:11434"}
        self.assertEqual(configured_ollama_base_url(cfg), "http://192.168.1.X:11434")


if __name__ == "__main__":
    unittest.main()
