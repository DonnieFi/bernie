"""chat_general subscription path: success, fail-closed, shadow (review follow-up)."""
from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from completion_router import (
    CompletionError,
    CompletionErrorCode,
    CompletionResult,
)


def _sub_cfg():
    return {
        "timezone": "America/Halifax",
        "subscription_models": [{
            "provider": "grok",
            "model": "grok-4.5",
            "capabilities": ["text"],
            "openrouter_fallback_model": "x-ai/grok-4.5",
            "enabled": True,
        }],
        "ollama_models": ["qwen-local"],
        "ollama_base_url": "http://192.168.1.X:11434",
        "llm_fallback": {"model": "qwen-local"},
        "eval": {"shadow_model": "or-shadow"},
    }


def _entry():
    from completion_router import ModelCatalogEntry, ProviderReadiness
    return ModelCatalogEntry(
        model="grok-4.5",
        provider="grok",
        capabilities=frozenset({"text"}),
        openrouter_fallback_model="x-ai/grok-4.5",
        enabled=True,
        readiness=ProviderReadiness.UNKNOWN,
    )


def _codex_entry():
    from completion_router import ModelCatalogEntry, ProviderReadiness
    return ModelCatalogEntry(
        model="gpt-5.4",
        provider="codex",
        capabilities=frozenset({"text", "tools"}),
        openrouter_fallback_model="openai/gpt-5.4",
        enabled=True,
        readiness=ProviderReadiness.UNKNOWN,
    )


class TestChatSubscription(unittest.IsolatedAsyncioTestCase):
    async def _run_chat(
        self,
        *,
        chain_result,
        active_model="grok-4.5",
        entry=None,
        resolved_tools=([], []),
    ):
        container = MagicMock()
        container.calendar = None
        container.session = None
        ctx = SimpleNamespace(mode=SimpleNamespace(slug="concierge"), render_blocks=lambda: [])

        chain_mock = AsyncMock(return_value=chain_result)
        tool_chain_mock = AsyncMock(return_value=chain_result)
        import subscription_complete

        with (
            patch("llm.runtime.get_container", return_value=container),
            patch("llm.chat.get_model_info", return_value=(active_model, None)),
            patch("modes.load_all_modes"),
            patch("modes.resolve_mode", return_value=SimpleNamespace(slug="concierge")),
            patch("modes.get_mode_override", return_value=None),
            patch("notification_router._is_quiet_hours", return_value=False),
            patch("context.BernieContext.build", new=AsyncMock(return_value=ctx)),
            patch("llm.context_builder.build_context", new=AsyncMock(return_value={})),
            patch("llm.chat._resolve_turn_tools", return_value=resolved_tools),
            patch("llm.chat._prepare_messages", return_value=[{"role": "user", "content": "hi"}]),
            patch("model_catalog.is_subscription_enabled", return_value=True),
            patch("completion_router.subscription_model", return_value=entry or _entry()),
            patch.object(subscription_complete, "complete_subscription_chain", chain_mock),
            patch.object(
                subscription_complete,
                "complete_subscription_with_tools",
                tool_chain_mock,
            ),
            patch.object(subscription_complete, "log_subscription_attempts", AsyncMock()),
            patch("llm.chat.get_db", return_value=MagicMock()),
            patch("llm.chat.maybe_fire_shadow") as shadow,
            patch("llm.chat._run_loop", new=AsyncMock()) as run_loop,
        ):
            from llm.chat import chat_general

            out = await chat_general("hi", [], _sub_cfg(), channel_id="1")
        return out, shadow, run_loop, chain_mock, tool_chain_mock

    async def test_subscription_success_returns_text_and_fires_shadow(self):
        ok = CompletionResult(provider="grok", model="grok-4.5", text="hello sub")
        out, shadow, run_loop, chain_mock, _ = await self._run_chat(chain_result=(ok, []))
        self.assertEqual(out, "hello sub")
        chain_mock.assert_awaited_once()
        shadow.assert_called_once()
        run_loop.assert_not_awaited()

    async def test_subscription_failure_is_fail_closed(self):
        fail = CompletionResult(
            provider="grok",
            model="grok-4.5",
            error=CompletionError(CompletionErrorCode.UNAVAILABLE, "down", retryable=True),
        )
        out, shadow, run_loop, _chain, _ = await self._run_chat(chain_result=(fail, []))
        self.assertIn("couldn't finish", out.lower())
        shadow.assert_not_called()
        run_loop.assert_not_awaited()

    async def test_codex_keeps_canonical_resolved_tool_surface(self):
        tools = [
            {"name": "get_current_weather", "input_schema": {"type": "object"}},
            {"name": "get_person_location", "input_schema": {"type": "object"}},
            {"name": "get_network_speedtest", "input_schema": {"type": "object"}},
        ]
        ok = CompletionResult(provider="codex", model="gpt-5.4", text="done")

        with patch(
            "llm.intent_router.narrow_tool_domains_for_subscription",
        ) as extra_narrow:
            out, _, _, chain_mock, tool_chain_mock = await self._run_chat(
                chain_result=(ok, []),
                active_model="gpt-5.4",
                entry=_codex_entry(),
                resolved_tools=(tools, ["weather", "presence", "network"]),
            )

        self.assertEqual(out, "done")
        chain_mock.assert_not_awaited()
        extra_narrow.assert_not_called()
        request = tool_chain_mock.await_args.args[0]
        self.assertEqual(request.tools, tuple(tools))

    async def test_subscription_success_when_get_db_unwired(self):
        ok = CompletionResult(provider="grok", model="grok-4.5", text="hello sub")
        container = MagicMock()
        container.calendar = None
        container.session = None
        ctx = SimpleNamespace(mode=SimpleNamespace(slug="concierge"), render_blocks=lambda: [])
        chain_mock = AsyncMock(return_value=(ok, []))
        import subscription_complete

        with (
            patch("llm.runtime.get_container", return_value=container),
            patch("llm.chat.get_model_info", return_value=("grok-4.5", None)),
            patch("modes.load_all_modes"),
            patch("modes.resolve_mode", return_value=SimpleNamespace(slug="concierge")),
            patch("modes.get_mode_override", return_value=None),
            patch("notification_router._is_quiet_hours", return_value=False),
            patch("context.BernieContext.build", new=AsyncMock(return_value=ctx)),
            patch("llm.context_builder.build_context", new=AsyncMock(return_value={})),
            patch("llm.chat._resolve_turn_tools", return_value=([], [])),
            patch("llm.chat._prepare_messages", return_value=[{"role": "user", "content": "hi"}]),
            patch("model_catalog.is_subscription_enabled", return_value=True),
            patch("completion_router.subscription_model", return_value=_entry()),
            patch.object(subscription_complete, "complete_subscription_chain", chain_mock),
            patch.object(subscription_complete, "log_subscription_attempts", AsyncMock()),
            patch("llm.chat.get_db", side_effect=RuntimeError("database not wired")),
            patch("llm.chat.maybe_fire_shadow") as shadow,
            patch("llm.chat._run_loop", new=AsyncMock()) as run_loop,
        ):
            from llm.chat import chat_general
            out = await chat_general("hi", [], _sub_cfg(), channel_id="1")
        self.assertEqual(out, "hello sub")
        shadow.assert_called_once()
        run_loop.assert_not_awaited()

    def test_failed_health_prefetch_keeps_watch_in_source(self):
        """Early prefetch failure must not clear health_sleep_watch before _run_loop."""
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "llm" / "chat.py"
        text = src.read_text(encoding="utf-8")
        self.assertNotIn(
            "health_sleep_watch, health_sleep_prefetch_ok = None, False, False",
            text,
        )
        start = text.index("if _health_task is not None:")
        block = text[start : start + 1200]
        self.assertIn("health_sleep_watch = True", block)

    async def test_web_model_arg_does_not_call_llm_for(self):
        """Explicit model= (webui/OpenWebUI) must not crash via _base_url_for_model → llm_for."""
        ok = CompletionResult(provider="grok", model="grok-4.5", text="web ok")
        container = MagicMock()
        container.calendar = None
        container.session = None
        container.llm_for.side_effect = ValueError(
            "subscription model 'grok-4.5' must route through CompletionRouter"
        )
        chain_mock = AsyncMock(return_value=(ok, []))
        import subscription_complete

        with (
            patch("llm.runtime.get_container", return_value=container),
            patch("llm.model_state._container", container),
            patch("llm.chat.get_model_info", return_value=("claude-sonnet", None)),
            patch("modes.load_all_modes"),
            patch("modes.resolve_mode", return_value=SimpleNamespace(slug="concierge")),
            patch("modes.get_mode_override", return_value=None),
            patch("notification_router._is_quiet_hours", return_value=False),
            patch(
                "context.BernieContext.build",
                new=AsyncMock(return_value=SimpleNamespace(
                    mode=SimpleNamespace(slug="concierge"), render_blocks=lambda: [],
                )),
            ),
            patch("llm.context_builder.build_context", new=AsyncMock(return_value={})),
            patch("llm.chat._resolve_turn_tools", return_value=([], [])),
            patch("llm.chat._prepare_messages", return_value=[{"role": "user", "content": "hi"}]),
            patch("model_catalog.is_subscription_enabled", return_value=True),
            patch("completion_router.subscription_model", return_value=_entry()),
            patch.object(subscription_complete, "complete_subscription_chain", chain_mock),
            patch.object(subscription_complete, "log_subscription_attempts", AsyncMock()),
            patch("llm.chat.get_db", return_value=MagicMock()),
            patch("llm.chat.maybe_fire_shadow"),
            patch("config.config", _sub_cfg()),
        ):
            from llm.chat import chat_general
            out = await chat_general(
                "hi", [], _sub_cfg(), model="grok-4.5", triggered_by="web",
            )
        self.assertEqual(out, "web ok")
        chain_mock.assert_awaited_once()
        container.llm_for.assert_not_called()


if __name__ == "__main__":
    unittest.main()
