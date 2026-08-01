"""Tests for Grok/subscription fallback chain (family-bot-tho.3)."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from completion_router import (
    CompletionError,
    CompletionErrorCode,
    CompletionRequest,
    CompletionResult,
    TokenUsage,
)
from subscription_complete import complete_subscription_chain, is_grok_subscription_model


def _cfg(**updates):
    cfg = {
        "ollama_models": ["qwen-local"],
        "ollama_base_url": "http://192.168.1.X:11434",
        "llm_fallback": {"model": "qwen-local"},
        "subscription_models": [{
            "provider": "grok",
            "model": "grok-4.5",
            "capabilities": ["text"],
            "openrouter_fallback_model": "x-ai/grok-4.5",
            "enabled": True,
        }],
        "subscription_runner_url": "http://runner.test:8080",
    }
    cfg.update(updates)
    return cfg


class TestSubscriptionComplete(unittest.IsolatedAsyncioTestCase):
    def test_is_grok_subscription_model(self):
        self.assertTrue(is_grok_subscription_model("grok-4.5", _cfg()))
        self.assertFalse(is_grok_subscription_model("claude-sonnet", _cfg()))
        disabled = _cfg()
        disabled["subscription_models"][0]["enabled"] = False
        self.assertFalse(is_grok_subscription_model("grok-4.5", disabled))

    async def test_chain_uses_grok_first_success(self):
        req = CompletionRequest(
            surface="chat", provider="grok", model="grok-4.5", prompt="hi",
        )
        grok_ok = CompletionResult(
            provider="grok", model="grok-4.5", text="hello",
            usage=TokenUsage(1, 2), latency_ms=10,
        )
        with patch(
            "subscription_complete._post_runner",
            new=AsyncMock(return_value=grok_ok),
        ) as post, patch(
            "subscription_complete._openrouter_text",
            new=AsyncMock(),
        ) as or_call, patch(
            "subscription_complete._ollama_text",
            new=AsyncMock(),
        ) as ol_call:
            result, attempts = await complete_subscription_chain(req, _cfg())

        self.assertEqual(result.text, "hello")
        self.assertEqual(len(attempts), 1)
        self.assertTrue(attempts[0]["ok"])
        self.assertEqual(attempts[0]["provider"], "grok")
        post.assert_awaited_once()
        or_call.assert_not_awaited()
        ol_call.assert_not_awaited()

    async def test_chain_posts_codex_primary_to_runner(self):
        cfg = _cfg()
        cfg["subscription_models"][0].update(provider="codex", model="gpt-5.4")
        req = CompletionRequest(
            surface="research", provider="codex", model="gpt-5.4", prompt="hi",
            tools=({"name": "echo"},), output_schema={"type": "object"},
        )
        codex_ok = CompletionResult(provider="codex", model="gpt-5.4", text="done")
        with patch(
            "subscription_complete._post_runner", new=AsyncMock(return_value=codex_ok)
        ) as post:
            result, attempts = await complete_subscription_chain(req, cfg)
        self.assertEqual(result.text, "done")
        self.assertEqual(attempts[0]["provider"], "codex")
        post.assert_awaited_once()
        forwarded = post.await_args.args[2]
        self.assertEqual(forwarded.tools, req.tools)
        self.assertEqual(forwarded.output_schema, req.output_schema)

    async def test_chain_falls_through_retryable_to_openrouter(self):
        req = CompletionRequest(
            surface="chat", provider="grok", model="grok-4.5", prompt="hi",
        )
        busy = CompletionResult(
            provider="grok",
            model="grok-4.5",
            error=CompletionError(CompletionErrorCode.BUSY, retryable=True),
        )
        or_ok = CompletionResult(
            provider="openrouter", model="x-ai/grok-4.5", text="from-or",
        )
        with patch(
            "subscription_complete._post_runner",
            new=AsyncMock(return_value=busy),
        ), patch(
            "subscription_complete._openrouter_text",
            new=AsyncMock(return_value=or_ok),
        ) as or_call, patch(
            "subscription_complete._ollama_text",
            new=AsyncMock(),
        ) as ol_call:
            result, attempts = await complete_subscription_chain(req, _cfg())

        self.assertEqual(result.text, "from-or")
        self.assertEqual([a["provider"] for a in attempts], ["grok", "openrouter"])
        self.assertFalse(attempts[0]["ok"])
        self.assertTrue(attempts[1]["ok"])
        or_call.assert_awaited_once()
        ol_call.assert_not_awaited()

    async def test_nonretryable_stops_without_ollama(self):
        req = CompletionRequest(
            surface="chat", provider="grok", model="grok-4.5", prompt="hi",
        )
        bad = CompletionResult(
            provider="grok",
            model="grok-4.5",
            error=CompletionError(
                CompletionErrorCode.INVALID_REQUEST, "bad", retryable=False
            ),
        )
        with patch(
            "subscription_complete._post_runner",
            new=AsyncMock(return_value=bad),
        ), patch(
            "subscription_complete._openrouter_text",
            new=AsyncMock(),
        ) as or_call, patch(
            "subscription_complete._ollama_text",
            new=AsyncMock(),
        ) as ol_call:
            result, attempts = await complete_subscription_chain(req, _cfg())

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, CompletionErrorCode.INVALID_REQUEST)
        self.assertEqual(len(attempts), 1)
        or_call.assert_not_awaited()
        ol_call.assert_not_awaited()

    async def test_openrouter_only_chain_when_no_ollama(self):
        cfg = _cfg()
        cfg["ollama_models"] = []
        cfg["llm_fallback"] = {}
        req = CompletionRequest(
            surface="chat", provider="grok", model="grok-4.5", prompt="hi",
        )
        busy = CompletionResult(
            provider="grok",
            model="grok-4.5",
            error=CompletionError(CompletionErrorCode.BUSY, retryable=True),
        )
        or_ok = CompletionResult(
            provider="openrouter", model="x-ai/grok-4.5", text="from-or",
        )
        with patch(
            "subscription_complete._post_runner",
            new=AsyncMock(return_value=busy),
        ), patch(
            "subscription_complete._openrouter_text",
            new=AsyncMock(return_value=or_ok),
        ) as or_call, patch(
            "subscription_complete._ollama_text",
            new=AsyncMock(),
        ) as ol_call:
            result, attempts = await complete_subscription_chain(req, cfg)

        self.assertEqual(result.text, "from-or")
        self.assertEqual([a["provider"] for a in attempts], ["grok", "openrouter"])
        or_call.assert_awaited_once()
        ol_call.assert_not_awaited()

    async def test_tool_loop_skips_openrouter_fallback(self):
        """Within a tool-bearing chain hop, OR/Ollama are skipped (tools need runner)."""
        cfg = _cfg()
        req = CompletionRequest(
            surface="chat",
            provider="grok",
            model="grok-4.5",
            prompt="weather",
            tools=({"name": "get_weather", "input_schema": {}},),
        )
        grok_fail = CompletionResult(
            provider="grok",
            model="grok-4.5",
            error=CompletionError(CompletionErrorCode.UNAVAILABLE, "down", retryable=True),
        )
        with patch(
            "subscription_complete._post_runner",
            new=AsyncMock(return_value=grok_fail),
        ), patch(
            "subscription_complete._openrouter_text",
            new=AsyncMock(),
        ) as or_call, patch(
            "subscription_complete._ollama_text",
            new=AsyncMock(),
        ) as ol_call:
            result, attempts = await complete_subscription_chain(req, cfg)

        or_call.assert_not_awaited()
        ol_call.assert_not_awaited()
        self.assertTrue(any(a.get("skipped") for a in attempts))
        self.assertIsNotNone(result.error)

    async def test_tool_loop_failure_returns_error_no_text_degrade(self):
        """Tool-loop runner failure must not strip tools and hallucinate via OpenRouter."""
        from subscription_complete import complete_subscription_with_tools

        cfg = _cfg()
        req = CompletionRequest(
            surface="chat",
            provider="grok",
            model="grok-4.5",
            prompt="weather",
            tools=({"name": "get_weather", "input_schema": {}},),
        )
        grok_fail = CompletionResult(
            provider="grok",
            model="grok-4.5",
            error=CompletionError(CompletionErrorCode.UNAVAILABLE, "down", retryable=True),
        )

        with patch(
            "subscription_complete._post_runner",
            new=AsyncMock(return_value=grok_fail),
        ) as runner_call, patch(
            "subscription_complete._openrouter_text",
            new=AsyncMock(),
        ) as or_call:
            result, attempts = await complete_subscription_with_tools(
                req, cfg, execute_tool=AsyncMock(return_value="ok"),
            )

        self.assertIsNotNone(result.error)
        self.assertFalse(any(a.get("phase") == "text_degrade" for a in attempts))
        runner_call.assert_awaited_once()
        or_call.assert_not_awaited()

    async def test_tool_execute_respects_wall_clock_bound(self):
        from subscription_complete import complete_subscription_with_tools
        from completion_router import ToolCall

        req = CompletionRequest(
            surface="chat",
            provider="grok",
            model="grok-4.5",
            prompt="slow tool",
            tools=({"name": "slow", "input_schema": {}},),
            timeout_s=5,
        )
        tool_step = CompletionResult(
            provider="grok",
            model="grok-4.5",
            tool_calls=(ToolCall("1", "slow", {}),),
        )
        captured_messages: list[dict] = []
        chain_calls = {"n": 0}

        async def chain_side(step_req, cfg, **kwargs):
            chain_calls["n"] += 1
            if chain_calls["n"] == 1:
                return tool_step, []
            captured_messages.extend(step_req.messages)
            return CompletionResult(provider="grok", model="grok-4.5", text="done"), []

        async def slow_tool(name, args):
            await asyncio.sleep(2)
            return "late"

        with patch(
            "subscription_complete.complete_subscription_chain",
            new=AsyncMock(side_effect=chain_side),
        ), patch(
            "subscription_complete._DEFAULT_TOOL_TIMEOUT_S",
            1,
        ):
            result, _ = await complete_subscription_with_tools(
                req, _cfg(), execute_tool=slow_tool, max_steps=3,
            )
        self.assertEqual(result.text, "done")
        tool_msgs = [
            m for m in captured_messages
            if m.get("role") == "user" and "Tool result" in str(m.get("content", ""))
        ]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("TimeoutError", tool_msgs[0]["content"])

    async def test_missing_fallback_tiers_returns_typed_error(self):
        cfg = _cfg()
        cfg["subscription_models"][0]["enabled"] = False
        req = CompletionRequest(
            surface="chat", provider="grok", model="grok-4.5", prompt="hi",
        )
        result, attempts = await complete_subscription_chain(req, cfg)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, CompletionErrorCode.UNAVAILABLE)
        self.assertEqual(attempts, [])

    async def test_tool_loop_executes_via_callback_only(self):
        from subscription_complete import complete_subscription_with_tools
        from completion_router import ToolCall

        req = CompletionRequest(
            surface="chat",
            provider="grok",
            model="grok-4.5",
            prompt="weather",
            tools=({"name": "get_weather", "input_schema": {}},),
        )
        tool_step = CompletionResult(
            provider="grok",
            model="grok-4.5",
            tool_calls=(ToolCall("1", "get_weather", {"city": "X"}),),
        )
        final = CompletionResult(provider="grok", model="grok-4.5", text="sunny")
        executed = []

        async def exec_tool(name, args):
            executed.append((name, args))
            return "ok-result"

        with patch(
            "subscription_complete.complete_subscription_chain",
            new=AsyncMock(side_effect=[(tool_step, [{"ok": True}]), (final, [{"ok": True}])]),
        ):
            result, attempts = await complete_subscription_with_tools(
                req, _cfg(), execute_tool=exec_tool, max_steps=3
            )
        self.assertEqual(result.text, "sunny")
        self.assertEqual(executed, [("get_weather", {"city": "X"})])
        self.assertEqual(len(attempts), 2)

    async def test_codex_tool_loop_resumes_native_app_server_turn(self):
        from subscription_complete import complete_subscription_with_tools
        from completion_router import ToolCall

        cfg = _cfg()
        cfg["subscription_models"][0].update(provider="codex", model="gpt-5.4")
        req = CompletionRequest(
            surface="chat",
            provider="codex",
            model="gpt-5.4",
            prompt="weather",
            tools=({"name": "get_weather", "input_schema": {}},),
        )
        tool_step = CompletionResult(
            provider="codex",
            model="gpt-5.4",
            tool_calls=(ToolCall("call-1", "get_weather", {"city": "Halifax"}),),
            continuation_id="resume-secret",
        )
        final = CompletionResult(provider="codex", model="gpt-5.4", text="sunny")
        seen_requests = []

        async def chain(step_req, _cfg, **_kwargs):
            seen_requests.append(step_req)
            return (tool_step, [{"ok": True}]) if len(seen_requests) == 1 else (final, [{"ok": True}])

        with patch(
            "subscription_complete.complete_subscription_chain",
            new=AsyncMock(side_effect=chain),
        ):
            result, _ = await complete_subscription_with_tools(
                req,
                cfg,
                execute_tool=AsyncMock(return_value="22 C and sunny"),
                max_steps=3,
            )

        self.assertEqual(result.text, "sunny")
        resumed = seen_requests[1]
        self.assertEqual(resumed.continuation_id, "resume-secret")
        self.assertEqual(
            resumed.tool_results,
            ({"id": "call-1", "text": "22 C and sunny", "success": True},),
        )
        self.assertEqual(resumed.messages, req.messages)

    async def test_tool_step_limit(self):
        from subscription_complete import complete_subscription_with_tools
        from completion_router import ToolCall

        req = CompletionRequest(
            surface="chat",
            provider="grok",
            model="grok-4.5",
            prompt="loop",
            tools=({"name": "t", "input_schema": {}},),
        )
        tool_step = CompletionResult(
            provider="grok",
            model="grok-4.5",
            tool_calls=(ToolCall("1", "t", {}),),
        )

        async def exec_tool(name, args):
            return "x"

        with patch(
            "subscription_complete.complete_subscription_chain",
            new=AsyncMock(return_value=(tool_step, [{"ok": True}])),
        ):
            result, _ = await complete_subscription_with_tools(
                req, _cfg(), execute_tool=exec_tool, max_steps=1
            )
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, CompletionErrorCode.SCHEMA)

    async def test_openrouter_hop_resolves_alias(self):
        from subscription_complete import _openrouter_text

        class _Resp:
            status = 200

            async def json(self, content_type=None):
                return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Sess:
            def post(self, url, **kw):
                self.last_body = kw.get("json")
                return _Resp()

        sess = _Sess()
        req = CompletionRequest(
            surface="worker", provider="openrouter", model="or-grok-45", prompt="hi",
        )
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}, clear=False),
            patch(
                "openrouter_models.resolve_openrouter_slug",
                return_value="x-ai/grok-4.5",
            ) as resolve,
        ):
            result = await _openrouter_text(sess, "or-grok-45", req, _cfg())
        resolve.assert_called_once_with("or-grok-45", _cfg())
        self.assertEqual(sess.last_body["model"], "x-ai/grok-4.5")
        self.assertEqual(result.text, "ok")

    async def test_chain_timeout_budget_decreases_per_hop(self):
        cfg = _cfg()
        req = CompletionRequest(
            surface="worker",
            provider="grok",
            model="grok-4.5",
            prompt="hi",
            timeout_s=60,
        )
        slow = CompletionResult(
            provider="grok",
            model="grok-4.5",
            error=CompletionError(CompletionErrorCode.BUSY, retryable=True),
            latency_ms=55_000,
        )
        ok = CompletionResult(provider="openrouter", model="x-ai/grok-4.5", text="ok", latency_ms=500)
        runner_timeouts: list[int | None] = []
        or_timeouts: list[int | None] = []

        async def _post(sess, base, hop_req):
            runner_timeouts.append(hop_req.timeout_s)
            return slow

        async def _or(sess, model, hop_req, cfg):
            or_timeouts.append(hop_req.timeout_s)
            return ok

        with (
            patch("subscription_complete._post_runner", new=AsyncMock(side_effect=_post)),
            patch("subscription_complete._openrouter_text", new=AsyncMock(side_effect=_or)),
            patch("subscription_complete._ollama_text", new=AsyncMock()),
        ):
            result, _ = await complete_subscription_chain(req, cfg)
        self.assertEqual(result.text, "ok")
        self.assertEqual(runner_timeouts, [60])
        self.assertEqual(len(or_timeouts), 1)
        self.assertLessEqual(or_timeouts[0], 5)


if __name__ == "__main__":
    unittest.main()
