"""Ollama hop in subscription chain must not double-log or invent token zeros."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from completion_router import CompletionRequest


class TestOllamaSubscriptionTelemetry(unittest.IsolatedAsyncioTestCase):
    async def test_ollama_text_skips_log_llm_turn_and_forwards_usage(self):
        import llm.ollama as ollama_mod
        from subscription_complete import _ollama_text

        req = CompletionRequest(surface="digest", provider="ollama", model="qwen", prompt="hi")
        mock_call = AsyncMock(
            return_value=("answer", {"input_tokens": 3, "output_tokens": 5})
        )
        with patch.object(ollama_mod, "call_ollama", mock_call):
            result = await _ollama_text(
                req, {"ollama_models": ["qwen"]}, "qwen", session=object()
            )

        self.assertIsNone(result.error, msg=getattr(result.error, "message", None))
        self.assertEqual(result.text, "answer")
        self.assertEqual(result.usage.input_tokens, 3)
        self.assertEqual(result.usage.output_tokens, 5)
        mock_call.assert_awaited()
        self.assertTrue(mock_call.await_args.kwargs.get("skip_telemetry"))

    async def test_call_ollama_skip_telemetry_returns_none_usage(self):
        import llm.ollama as ollama_mod

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def json(self):
                return {"message": {"content": "hi"}}

            async def text(self):
                return ""

        class _Sess:
            closed = False

            def post(self, *a, **k):
                return _Resp()

        async def _qr(coro, *a, **k):
            return await coro

        with (
            patch("ollama_resolver.resolve_ollama_base_url", new=AsyncMock(return_value="http://o")),
            patch.object(ollama_mod, "log_llm_turn", new=AsyncMock()) as mock_log,
            patch("llm.queue.queued_run", new=_qr),
            patch("config.load_config", return_value={}),
            patch("llm.context_builder.build_context", new=AsyncMock(return_value={})),
            patch.object(ollama_mod, "format_content", side_effect=lambda c: c if isinstance(c, str) else str(c), create=True),
        ):
            # Avoid importing worker (discord/audioop) for SMALL_MODEL_DISCIPLINE.
            import types
            fake_worker = types.ModuleType("worker")
            fake_worker.SMALL_MODEL_DISCIPLINE = ""
            with patch.dict(sys.modules, {"worker": fake_worker}):
                out = await ollama_mod.call_ollama(
                    "sys",
                    [{"role": "user", "content": "q"}],
                    {"ollama_models": ["m"], "llm_fallback": {"model": "m"}},
                    _Sess(),
                    model_override="m",
                    skip_telemetry=True,
                )
        self.assertIsInstance(out, tuple)
        text, usage = out
        self.assertEqual(text, "hi")
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["output_tokens"])
        mock_log.assert_not_awaited()

    async def test_skip_telemetry_bypasses_llm_queue(self):
        import llm.ollama as ollama_mod

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def json(self):
                return {"message": {"content": "hi"}}

            async def text(self):
                return ""

        class _Sess:
            closed = False

            def post(self, *a, **k):
                return _Resp()

        queued = AsyncMock(side_effect=AssertionError("queued_run must not run"))
        import types
        fake_worker = types.ModuleType("worker")
        fake_worker.SMALL_MODEL_DISCIPLINE = ""
        with (
            patch("ollama_resolver.resolve_ollama_base_url", new=AsyncMock(return_value="http://o")),
            patch("llm.queue.queued_run", queued),
            patch("llm.context_builder.build_context", new=AsyncMock(return_value={})),
            patch.dict(sys.modules, {"worker": fake_worker}),
        ):
            out = await ollama_mod.call_ollama(
                "sys",
                [{"role": "user", "content": "q"}],
                {"ollama_models": ["m"], "llm_fallback": {"model": "m"}},
                _Sess(),
                model_override="m",
                skip_telemetry=True,
                timeout_s=120,
            )
        self.assertEqual(out[0], "hi")
        queued.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
