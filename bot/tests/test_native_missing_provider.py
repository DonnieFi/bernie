"""Native executor handles MissingProviderClient (family-bot-u3sq)."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from completion_router import MissingProviderClient
from executor import ExecutorConfig, ServiceRefs
from executors.native import NativeToolExecutor


class TestNativeMissingProvider(unittest.IsolatedAsyncioTestCase):
    async def test_missing_anthropic_returns_calm_message(self):
        gw = MagicMock()
        ex = NativeToolExecutor(gw)
        refs = ServiceRefs()

        def _llm_for(model: str):
            raise MissingProviderClient("anthropic", model)

        refs.llm_for = _llm_for
        ex.with_services(refs)
        cfg = ExecutorConfig(surface="chat", model="claude-sonnet", channel_id="1")
        with patch("config.config", {"ollama_models": [], "llm_fallback": {}}):
            out = await ex.run(
                [{"role": "user", "content": "hi"}],
                "sys",
                [],
                cfg,
            )
        self.assertIn("couldn't reach", out.lower())

    async def test_missing_anthropic_falls_back_to_ollama(self):
        gw = MagicMock()
        ex = NativeToolExecutor(gw)
        refs = ServiceRefs()

        def _llm_for(model: str):
            raise MissingProviderClient("anthropic", model)

        refs.llm_for = _llm_for
        ex.with_services(refs)
        cfg = ExecutorConfig(surface="chat", model="claude-sonnet", channel_id="1")
        app_cfg = {
            "ollama_models": ["qwen-local"],
            "ollama_base_url": "http://192.168.1.X:11434",
            "llm_fallback": {"model": "qwen-local"},
        }
        with (
            patch("config.config", app_cfg),
            patch("llm.ollama.call_ollama", new=AsyncMock(return_value="ollama-ok")) as ollama,
        ):
            out = await ex.run(
                [{"role": "user", "content": "hi"}],
                "sys",
                [],
                cfg,
            )
        self.assertEqual(out, "ollama-ok")
        ollama.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
