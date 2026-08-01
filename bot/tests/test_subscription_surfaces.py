"""Surface execution routing for subscription models (family-bot-3an.5).

Each named acceptance surface must invoke complete_subscription_chain (via
subscription_invoke) rather than llm_for / Pydantic factories that reject
Codex/Grok. Mocks only — no live completions.
"""
from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _sub_cfg(**extra):
    cfg = {
        "anthropic_models": ["claude-sonnet"],
        "litellm_models": ["or-deepseek"],
        "ollama_models": ["qwen-local"],
        "llm_fallback": {"model": "qwen-local"},
        "subscription_models": [
            {
                "provider": "grok",
                "model": "grok-4.5",
                "capabilities": ["text", "tools"],
                "openrouter_fallback_model": "x-ai/grok-4.5",
                "enabled": True,
            },
        ],
        "subscription_runner_url": "http://runner.test:8080",
        "digest_model": "grok-4.5",
        "audit_model": "grok-4.5",
        "primary_reliable_model": "grok-4.5",
        "eval": {
            "eval_model": "grok-4.5",
            "shadow_model": "grok-4.5",
            "worker_model": "grok-4.5",
            "judge_fallback_model": "or-deepseek",
            "judge_ollama_fallback": "qwen-local",
        },
        "cognitive_workers": {
            "research": {"default_model": "grok-4.5", "upgrade_model": "grok-4.5"},
            "study_guide": {"default_model": "grok-4.5"},
            "reflection": {"default_model": "grok-4.5"},
            "consolidation": {"default_model": "grok-4.5"},
        },
    }
    cfg.update(extra)
    return cfg


class TestSubscriptionSurfaceRouting(unittest.IsolatedAsyncioTestCase):
    async def test_complete_text_uses_subscription_chain(self):
        from subscription_invoke import complete_text
        from completion_router import CompletionResult

        fake = CompletionResult(text="ok-surface", error=None, provider="grok", model="grok-4.5")
        with (
            patch(
                "subscription_complete.complete_subscription_chain",
                new=AsyncMock(return_value=(fake, [{"provider": "grok", "ok": True}])),
            ) as mock_chain,
            patch("subscription_complete.log_subscription_attempts", new=AsyncMock()),
        ):
            out = await complete_text(
                "grok-4.5", config=_sub_cfg(), prompt="hi", surface="digest",
            )
        self.assertEqual(out, "ok-surface")
        mock_chain.assert_awaited()
        req = mock_chain.await_args.args[0]
        self.assertEqual(req.surface, "digest")
        self.assertEqual(req.model, "grok-4.5")
        self.assertEqual(
            mock_chain.await_args.kwargs.get("surface_ollama_model"),
            None,
        )

    async def test_eval_surface_uses_judge_ollama_fallback(self):
        from subscription_invoke import complete_text
        from completion_router import CompletionResult

        cfg = _sub_cfg(
            ollama_models=["qwen-local", "judge-local"],
            llm_fallback={"model": "qwen-local"},
            eval={
                "eval_model": "grok-4.5",
                "judge_ollama_fallback": "judge-local",
            },
        )
        with (
            patch(
                "subscription_complete.complete_subscription_chain",
                new=AsyncMock(return_value=(
                    CompletionResult(text="ok", provider="grok", model="grok-4.5"),
                    [],
                )),
            ) as mock_chain,
            patch("subscription_complete.log_subscription_attempts", new=AsyncMock()),
        ):
            await complete_text(
                "grok-4.5", config=cfg, prompt="hi", surface="eval",
            )
        self.assertEqual(
            mock_chain.await_args.kwargs.get("surface_ollama_model"),
            "judge-local",
        )

    async def test_worker_shared_routes_subscription(self):
        from cognitive_handlers.worker_shared import call_worker_model

        with (
            patch("config.config", _sub_cfg()),
            patch(
                "subscription_invoke.complete_text",
                new=AsyncMock(return_value="worker-ok"),
            ) as mock_ct,
            patch(
                "cognitive_handlers.worker_shared.call_ollama_fallback",
                new=AsyncMock(return_value="ollama"),
            ),
        ):
            out = await call_worker_model("topic")
        self.assertEqual(out, "worker-ok")
        mock_ct.assert_awaited()
        self.assertEqual(mock_ct.await_args.kwargs.get("surface"), "worker")

    async def test_audit_routes_subscription(self):
        from llm.audit import call_for_audit

        with patch(
            "subscription_invoke.complete_text",
            new=AsyncMock(return_value="audited"),
        ) as mock_ct:
            out = await call_for_audit("draft body", _sub_cfg(), container=None)
        self.assertEqual(out, "audited")
        mock_ct.assert_awaited()
        self.assertEqual(mock_ct.await_args.kwargs.get("surface"), "audit")

    async def test_shadow_routes_subscription(self):
        from eval.shadow import _call_shadow_model

        with patch(
            "subscription_invoke.complete_text",
            new=AsyncMock(return_value="shadow-ok"),
        ) as mock_ct:
            text, tin, tout = await _call_shadow_model(
                "grok-4.5",
                "sys",
                [{"role": "user", "content": "q"}],
                config=_sub_cfg(),
            )
        self.assertEqual(text, "shadow-ok")
        self.assertIsNone(tin)
        mock_ct.assert_awaited()
        self.assertEqual(mock_ct.await_args.kwargs.get("surface"), "shadow")

    async def test_cognitive_call_text_routes_subscription(self):
        from cognitive_workers import CognitiveWorkerBase

        class _W(CognitiveWorkerBase):
            name = "reflection"
            default_model = "grok-4.5"

        w = _W()
        with patch(
            "subscription_invoke.complete_text",
            new=AsyncMock(return_value="reflect-ok"),
        ) as mock_ct:
            text, stats = await w.call_text(
                _sub_cfg(), "prompt", system="sys", model="grok-4.5",
            )
        self.assertEqual(text, "reflect-ok")
        self.assertEqual(stats.get("provider"), "subscription")
        mock_ct.assert_awaited()

    async def test_study_guide_uses_call_text(self):
        from cognitive_workers.study_guide import StudyGuideWorker

        cfg = _sub_cfg()
        w = StudyGuideWorker(cfg)
        db = MagicMock()
        db.store_task_output = AsyncMock()
        db.create_cognitive_task = AsyncMock()
        container = SimpleNamespace(db=db)
        with patch.object(
            StudyGuideWorker, "call_text", new=AsyncMock(return_value=("guide md", {"model": "grok-4.5"}))
        ) as mock_ct:
            await w.handle(
                {
                    "id": 1,
                    "payload": {
                        "event_id": "e1",
                        "person_id": "p1",
                        "summary": "Math",
                        "description": "quiz",
                        "start": "2099-01-01T12:00:00+00:00",
                    },
                },
                container,
            )
        mock_ct.assert_awaited()
        db.store_task_output.assert_awaited()

    async def test_judge_subscription_tier(self):
        from eval.judges import _run_judge_with_fallbacks
        from pydantic import BaseModel, Field

        class _R(BaseModel):
            score: float = Field(ge=0, le=1)

        parsed = _R(score=0.9)
        with (
            patch("config.config", _sub_cfg()),
            patch(
                "subscription_invoke.complete_typed",
                new=AsyncMock(return_value=parsed),
            ) as mock_typed,
        ):
            result, model_used = await _run_judge_with_fallbacks(
                "judge prompt", _R, "grok-4.5", "judge_pair",
            )
        self.assertEqual(model_used, "grok-4.5")
        self.assertEqual(result.output.score, 0.9)
        mock_typed.assert_awaited()

    async def test_llm_for_rejects_subscription(self):
        from service_container import ServiceContainer

        c = ServiceContainer(anthropic=object(), litellm=object(), ollama="http://o")
        with patch("config.config", _sub_cfg()), self.assertRaises(ValueError):
            c.llm_for("grok-4.5")


if __name__ == "__main__":
    unittest.main()
