"""Subscription telemetry contract (family-bot-3an.4).

- exact attempt order
- one DB record per eligible attempt (provider-reported usage only)
- one Langfuse observation per attempt (not generation + log_llm_turn)
- one parent route trace for all attempts
- unknown usage omitted (never zeros)
- secrets/prompts/device codes/account identity never appear
- provider metadata distinguishable (subscription / openrouter / ollama)
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _attempts():
    return [
        {
            "attempt": 1,
            "provider": "grok",
            "model": "grok-4.5",
            "ok": False,
            "error_code": "unavailable",
            "retryable": True,
            "latency_ms": 12.0,
            "selected_primary": "grok-4.5",
            "input_tokens": None,
            "output_tokens": None,
        },
        {
            "attempt": 2,
            "provider": "openrouter",
            "model": "x-ai/grok-4.5",
            "ok": True,
            "error_code": None,
            "retryable": None,
            "latency_ms": 40.0,
            "selected_primary": "grok-4.5",
            "input_tokens": 11,
            "output_tokens": 7,
        },
        {
            "attempt": 3,
            "provider": "ollama",
            "model": "qwen-local",
            "ok": False,
            "error_code": "timeout",
            "retryable": True,
            "latency_ms": 5.0,
            "selected_primary": "grok-4.5",
            "input_tokens": None,
            "output_tokens": None,
        },
    ]


class TestSubscriptionTelemetry(unittest.IsolatedAsyncioTestCase):
    async def test_one_parent_trace_one_observation_per_attempt(self):
        from subscription_complete import log_subscription_attempts

        gen_calls = []
        create_calls = []
        db_calls = []

        async def _create_trace(**kwargs):
            create_calls.append(kwargs)
            return "parent-trace-abc"

        async def _log_gen(**kwargs):
            gen_calls.append(kwargs)
            return kwargs.get("trace_id")

        async def _db_routed(name, **kwargs):
            db_calls.append((name, kwargs))

        with (
            patch("langfuse_logger.create_trace", new=_create_trace),
            patch("langfuse_logger.log_generation", new=_log_gen),
            patch("langfuse_logger.new_trace_id", return_value="parent-trace-abc"),
            patch("db_writes.routed", new=_db_routed),
        ):
            summary = await log_subscription_attempts(
                _attempts(),
                user_input="SECRET PROMPT DO NOT LOG",
                final_text="answer",
                triggered_by="discord",
                surface="chat",
                actor_id="person:test",
            )

        self.assertEqual(summary["trace_id"], "parent-trace-abc")
        self.assertEqual(summary["observations"], 3)
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0]["name"], "subscription_route")
        # One generation per attempt, all under parent, create_trace=False
        self.assertEqual(len(gen_calls), 3)
        for g in gen_calls:
            self.assertEqual(g["trace_id"], "parent-trace-abc")
            self.assertFalse(g["create_trace"])
            self.assertEqual(g["name"], "subscription_attempt")
        # Order preserved
        self.assertEqual(
            [g["metadata"]["provider"] for g in gen_calls],
            ["grok", "openrouter", "ollama"],
        )
        self.assertEqual(
            [g["metadata"]["attempt"] for g in gen_calls],
            ["1", "2", "3"],
        )

    async def test_unknown_usage_omitted_known_persisted_once(self):
        from subscription_complete import log_subscription_attempts

        gen_calls = []
        db_calls = []

        async def _log_gen(**kwargs):
            gen_calls.append(kwargs)

        async def _db_routed(name, **kwargs):
            db_calls.append((name, kwargs))

        with (
            patch("langfuse_logger.create_trace", new=AsyncMock(return_value="t1")),
            patch("langfuse_logger.log_generation", new=_log_gen),
            patch("langfuse_logger.new_trace_id", return_value="t1"),
            patch("db_writes.routed", new=_db_routed),
        ):
            summary = await log_subscription_attempts(_attempts())

        # Attempt 1 & 3: None tokens → omit (pass None, not 0)
        self.assertIsNone(gen_calls[0]["input_tokens"])
        self.assertIsNone(gen_calls[0]["output_tokens"])
        self.assertIsNone(gen_calls[2]["input_tokens"])
        # Attempt 2: reported usage
        self.assertEqual(gen_calls[1]["input_tokens"], 11)
        self.assertEqual(gen_calls[1]["output_tokens"], 7)
        # One DB row only (attempt 2)
        self.assertEqual(summary["db_rows"], 1)
        self.assertEqual(len(db_calls), 1)
        self.assertEqual(db_calls[0][0], "log_token_usage")
        self.assertEqual(db_calls[0][1]["input_tokens"], 11)
        self.assertEqual(db_calls[0][1]["model"], "openrouter/x-ai/grok-4.5")

    async def test_no_double_langfuse_via_log_llm_turn(self):
        from subscription_complete import log_subscription_attempts

        with (
            patch("langfuse_logger.create_trace", new=AsyncMock(return_value="t")),
            patch("langfuse_logger.log_generation", new=AsyncMock()) as mock_gen,
            patch("langfuse_logger.new_trace_id", return_value="t"),
            patch("db_writes.routed", new=AsyncMock()),
            patch("llm.observability.log_llm_turn", new=AsyncMock()) as mock_turn,
        ):
            await log_subscription_attempts(_attempts())
        # Exactly 3 generations; log_llm_turn never used (would double-count Langfuse)
        self.assertEqual(mock_gen.await_count, 3)
        mock_turn.assert_not_awaited()

    async def test_partial_usage_is_not_persisted_as_zero(self):
        from subscription_complete import log_subscription_attempts

        attempts = _attempts()
        attempts[0]["input_tokens"] = 11
        attempts[2]["output_tokens"] = 7
        with (
            patch("langfuse_logger.create_trace", new=AsyncMock(return_value="t")),
            patch("langfuse_logger.log_generation", new=AsyncMock()),
            patch("langfuse_logger.new_trace_id", return_value="t"),
            patch("db_writes.routed", new=AsyncMock()) as mock_db,
        ):
            summary = await log_subscription_attempts(attempts)

        self.assertEqual(summary["db_rows"], 1)
        mock_db.assert_awaited_once()

    async def test_secret_and_prompt_exclusion(self):
        from subscription_complete import log_subscription_attempts

        gen_calls = []
        create_calls = []

        async def _create(**kw):
            create_calls.append(kw)
            return "t"

        async def _gen(**kw):
            gen_calls.append(kw)

        dirty = _attempts()
        dirty[0]["token"] = "sk-live-secret"
        dirty[0]["device_code"] = "ABCD-1234"
        dirty[0]["email"] = "user@x.ai"
        dirty[0]["auth_file"] = "/home/red/.grok/auth.json"

        with (
            patch("langfuse_logger.create_trace", new=_create),
            patch("langfuse_logger.log_generation", new=_gen),
            patch("langfuse_logger.new_trace_id", return_value="t"),
            patch("db_writes.routed", new=AsyncMock()),
        ):
            await log_subscription_attempts(
                dirty,
                user_input="my private prompt with secrets",
                final_text="ok",
            )

        blob = repr(create_calls) + repr(gen_calls)
        for banned in (
            "sk-live-secret", "ABCD-1234", "user@x.ai",
            "auth.json", "my private prompt",
        ):
            self.assertNotIn(banned, blob)
        # Markers only
        self.assertEqual(create_calls[0]["user_input"], "[subscription-route]")
        self.assertEqual(gen_calls[0]["user_input"], "[subscription-attempt]")

    async def test_providers_distinguishable(self):
        from subscription_complete import log_subscription_attempts

        gen_calls = []

        async def _gen(**kw):
            gen_calls.append(kw)

        with (
            patch("langfuse_logger.create_trace", new=AsyncMock(return_value="t")),
            patch("langfuse_logger.log_generation", new=_gen),
            patch("langfuse_logger.new_trace_id", return_value="t"),
            patch("db_writes.routed", new=AsyncMock()),
        ):
            summary = await log_subscription_attempts(_attempts())

        self.assertEqual(summary["providers"], ["grok", "openrouter", "ollama"])
        models = [g["model"] for g in gen_calls]
        self.assertEqual(models[0], "grok/grok-4.5")
        self.assertEqual(models[1], "openrouter/x-ai/grok-4.5")
        self.assertEqual(models[2], "ollama/qwen-local")

    async def test_codex_oauth_is_openai_attempt_with_langfuse_observation(self):
        from subscription_complete import log_subscription_attempts

        attempts = [{
            "attempt": 1, "provider": "codex", "model": "gpt-5.4", "ok": True,
            "latency_ms": 10, "input_tokens": 11, "output_tokens": 7,
        }]
        with (
            patch("langfuse_logger.create_trace", new=AsyncMock(return_value="t")),
            patch("langfuse_logger.log_generation", new=AsyncMock()) as mock_gen,
            patch("langfuse_logger.new_trace_id", return_value="t"),
            patch("db_writes.routed", new=AsyncMock()) as mock_db,
        ):
            await log_subscription_attempts(attempts)

        self.assertEqual(mock_gen.await_args.kwargs["model"], "codex/gpt-5.4")
        self.assertEqual(mock_gen.await_args.kwargs["metadata"]["provider"], "codex")
        self.assertEqual(mock_db.await_args.kwargs["model"], "codex/gpt-5.4")

    def test_activity_classifies_oauth_and_openrouter_separately(self):
        import activity_aggregator

        cfg = {
            "subscription_models": [{
                "provider": "codex",
                "model": "gpt-5.4",
                "capabilities": ["text"],
                "openrouter_fallback_model": "openai/gpt-5.4",
                "enabled": True,
            }],
            "anthropic_models": ["claude-sonnet-5"],
        }
        with patch.object(activity_aggregator, "config", cfg):
            self.assertEqual(activity_aggregator._usage_provider("gpt-5.4"), "openai")
            self.assertEqual(activity_aggregator._usage_provider("codex/gpt-5.4"), "openai")
            self.assertEqual(
                activity_aggregator._usage_provider("openrouter/openai/gpt-5.4"),
                "openrouter",
            )
            self.assertEqual(
                activity_aggregator._usage_provider("claude-sonnet-5"),
                "anthropic",
            )


class TestLangfuseOmitUnknownUsage(unittest.IsolatedAsyncioTestCase):
    async def test_log_generation_omits_usage_when_none(self):
        from langfuse_logger import log_generation

        posted = []

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Sess:
            def post(self, url, **kwargs):
                posted.append(kwargs.get("json"))
                return _Resp()

        with (
            patch.dict(os.environ, {
                "LANGFUSE_PUBLIC_KEY": "pk",
                "LANGFUSE_SECRET_KEY": "sk",
                "LANGFUSE_HOST": "http://lf.test",
            }),
            patch("langfuse_logger.get_http_session", return_value=_Sess()),
        ):
            await log_generation(
                model="grok/grok-4.5",
                user_input="[subscription-attempt]",
                output="[ok]",
                input_tokens=None,
                output_tokens=None,
                create_trace=False,
                trace_id="parent",
                cache_creation_tokens=None,
                cache_read_tokens=None,
                metadata={"provider": "grok"},
            )

        self.assertEqual(len(posted), 1)
        batch = posted[0]["batch"]
        self.assertEqual(len(batch), 1)  # generation only
        body = batch[0]["body"]
        self.assertEqual(body["metadata"]["provider"], "grok")
        self.assertNotIn("usage", body)
        self.assertNotIn("usageDetails", body)

    async def test_log_generation_includes_known_usage(self):
        from langfuse_logger import log_generation

        posted = []

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Sess:
            def post(self, url, **kwargs):
                posted.append(kwargs.get("json"))
                return _Resp()

        with (
            patch.dict(os.environ, {
                "LANGFUSE_PUBLIC_KEY": "pk",
                "LANGFUSE_SECRET_KEY": "sk",
                "LANGFUSE_HOST": "http://lf.test",
            }),
            patch("langfuse_logger.get_http_session", return_value=_Sess()),
        ):
            await log_generation(
                model="openrouter/x",
                user_input="hi",
                output="yo",
                input_tokens=3,
                output_tokens=4,
            )

        body = posted[0]["batch"][1]["body"]  # generation after trace
        self.assertEqual(body["usage"]["input"], 3)
        self.assertEqual(body["usage"]["output"], 4)


if __name__ == "__main__":
    unittest.main()
