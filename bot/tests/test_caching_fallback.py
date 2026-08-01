"""family-bot-8z7: prompt-cache fallback matcher + preserve max_tokens on retry."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestPromptCacheErrorMatcher(unittest.TestCase):
    def test_matcher_narrow(self):
        from executors.native import _is_prompt_cache_error

        self.assertTrue(_is_prompt_cache_error("BadRequestError: cache_control is not supported"))
        self.assertTrue(_is_prompt_cache_error("Provider does not support prompt caching"))
        self.assertTrue(_is_prompt_cache_error("prompt cache not supported for this model"))
        self.assertTrue(_is_prompt_cache_error("caching is not supported by this model"))
        self.assertFalse(_is_prompt_cache_error("tool foo is not supported"))
        self.assertFalse(_is_prompt_cache_error("model not supported"))
        self.assertFalse(_is_prompt_cache_error("operation not supported"))


class TestCachingFallback(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from executor import ExecutorConfig, ServiceRefs
        from executors.native import NativeToolExecutor

        self.gateway = MagicMock()
        self.config = ExecutorConfig(
            surface="chat",
            model="claude-3-5-sonnet-20240620",
            conversation_id="test-conv",
        )
        self.mock_client = MagicMock()
        self.executor = NativeToolExecutor(self.gateway).with_services(
            ServiceRefs(llm_for=lambda _m: self.mock_client)
        )
        # Avoid real queue / config side effects
        self._queue_patch = patch(
            "llm.queue.queued_messages_create",
            new_callable=AsyncMock,
        )
        self.mock_create = self._queue_patch.start()

    async def asyncTearDown(self):
        self._queue_patch.stop()

    def _text_response(self, text: str):
        block = MagicMock()
        block.text = text
        block.type = "text"
        resp = MagicMock()
        resp.stop_reason = "end_turn"
        resp.content = [block]
        resp.usage = MagicMock(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        return resp

    async def test_fallback_strips_cache_and_keeps_max_tokens(self):
        error_400 = Exception("BadRequestError: 400 - cache_control is not supported by this model")
        ok = self._text_response("Fallback response")
        self.mock_create.side_effect = [error_400, ok]

        system = [{"type": "text", "text": "Static", "cache_control": {"type": "ephemeral"}}]
        with patch("llm.observability.log_llm_turn", new_callable=AsyncMock), \
             patch("config.config", {"timezone": "UTC", "executor": {"max_tokens": 4096}}):
            result = await self.executor.run(
                [{"role": "user", "content": "hi"}], system, [], self.config
            )

        self.assertEqual(result, "Fallback response")
        self.assertEqual(self.mock_create.await_count, 2)
        second = self.mock_create.await_args_list[1].kwargs
        self.assertEqual(second["max_tokens"], 4096)
        self.assertNotIn("cache_control", second["system"][0])

    async def test_fallback_persists_strip_across_tool_loop_steps(self):
        """After one cache rejection, later steps must not re-send cache_control."""
        error_400 = Exception("BadRequestError: cache_control is not supported")
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "t1"
        tool_block.name = "ping"
        tool_block.input = {}
        tool_resp = MagicMock()
        tool_resp.stop_reason = "tool_use"
        tool_resp.content = [tool_block]
        tool_resp.usage = MagicMock(
            input_tokens=10, output_tokens=5,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )
        final = self._text_response("done")
        # step1 fail → strip retry tool_use → step2 final (no second fail)
        self.mock_create.side_effect = [error_400, tool_resp, final]
        system = [{"type": "text", "text": "Static", "cache_control": {"type": "ephemeral"}}]
        self.gateway.execute = AsyncMock(return_value={"ok": True})
        with patch("llm.observability.log_llm_turn", new_callable=AsyncMock), \
             patch("config.config", {
                 "timezone": "UTC",
                 "executor": {"max_tokens": 4096, "max_steps": 5},
             }):
            result = await self.executor.run(
                [{"role": "user", "content": "hi"}],
                system,
                [{"name": "ping", "cache_control": {"type": "ephemeral"}}],
                self.config,
            )
        self.assertEqual(result, "done")
        self.assertEqual(self.mock_create.await_count, 3)
        # Only first call may include cache_control; retry + step2 must not
        for i, call in enumerate(self.mock_create.await_args_list):
            sys_arg = call.kwargs.get("system")
            if isinstance(sys_arg, list) and sys_arg:
                has_cc = "cache_control" in sys_arg[0]
                if i == 0:
                    self.assertTrue(has_cc)
                else:
                    self.assertFalse(has_cc, f"call {i} still had cache_control")

    async def test_bare_not_supported_does_not_retry(self):
        self.mock_create.side_effect = Exception("BadRequestError: tool xyz is not supported")
        system = [{"type": "text", "text": "Static", "cache_control": {"type": "ephemeral"}}]
        with patch("config.config", {"timezone": "UTC", "executor": {"max_tokens": 4096}}):
            with self.assertRaises(Exception):
                await self.executor.run(
                    [{"role": "user", "content": "hi"}], system, [], self.config
                )
        self.assertEqual(self.mock_create.await_count, 1)

    async def test_litellm_prompt_caching_error_retries(self):
        error_400 = Exception("LiteLLM Error: 400 - Provider does not support prompt caching")
        ok = self._text_response("LiteLLM Fallback")
        self.mock_create.side_effect = [error_400, ok]
        system = [{"type": "text", "text": "Static", "cache_control": {"type": "ephemeral"}}]
        with patch("config.config", {"timezone": "UTC", "executor": {"max_tokens": 4096}}):
            result = await self.executor.run(
                [{"role": "user", "content": "hi"}], system, [], self.config
            )
        self.assertEqual(result, "LiteLLM Fallback")
        self.assertEqual(self.mock_create.await_count, 2)
        self.assertEqual(self.mock_create.await_args_list[1].kwargs["max_tokens"], 4096)


if __name__ == "__main__":
    unittest.main()
