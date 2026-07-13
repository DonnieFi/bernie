"""Tests for llm.shadow_hooks deferred capture dispatch."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.policy import resolve_eval_policy


class TestMaybeFireShadow(unittest.TestCase):
    def test_short_circuits_without_shadow_model(self):
        from llm.shadow_hooks import maybe_fire_shadow

        with patch("telemetry.fire_and_forget") as mock_ff:
            maybe_fire_shadow(
                {},
                "hi",
                "sys",
                [{"role": "user", "content": "hi"}],
                "reply",
                None,
            )
        mock_ff.assert_not_called()

    def test_short_circuits_when_capture_disabled(self):
        from llm.shadow_hooks import maybe_fire_shadow

        config = {"eval": {"shadow_model": "or-shadow", "capture": {"enabled": False}}}
        with patch("telemetry.fire_and_forget") as mock_ff:
            maybe_fire_shadow(
                config,
                "hi",
                "sys",
                [{"role": "user", "content": "hi"}],
                "reply",
                None,
            )
        mock_ff.assert_not_called()


class TestFireShadowDeferred(unittest.IsolatedAsyncioTestCase):
    async def test_harness_on_passed_through_to_triplet(self):
        from llm.shadow_hooks import _fire_shadow_deferred

        config = {
            "eval": {
                "enabled": True,
                "shadow_model": "or-test-shadow",
                "harness": {"enabled": True},
            },
        }
        policy = resolve_eval_policy(config)
        kwargs = dict(
            policy=policy,
            harness_on=True,
            shed_on_backpressure=policy.shed_on_backpressure,
            config=config,
            user_message="hi",
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            primary_response="primary",
            channel_id="",
            actor_id="",
            cal_service=None,
            db_module=None,
            session=None,
            tz=None,
            model="claude-sonnet-4-6",
            group="family",
            triggered_by="discord",
            tool_domains=None,
        )
        with patch("eval_service.fire_shadow_triplet", new_callable=AsyncMock) as mock_triplet, \
             patch("eval_service.fire_shadow_call", new_callable=AsyncMock) as mock_pair, \
             patch("tool_gateway.get_tool_gateway", return_value=MagicMock()), \
             patch("llm.shadow_hooks.get_container", return_value=MagicMock()):
            await _fire_shadow_deferred(0, **kwargs)
        mock_triplet.assert_awaited_once()
        mock_pair.assert_not_called()
