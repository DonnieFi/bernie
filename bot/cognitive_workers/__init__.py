"""Cognitive worker base class — shared dispatch shape for Phase 26 workers.

Each worker subclass declares its model assignment, context budget, and runtime cap;
CognitiveWorker invokes `handle(task, container)` and persists the returned _stats dict
to cognitive_tasks via complete_cognitive_task_with_stats.
"""
from __future__ import annotations

import logging
from typing import Optional


def merge_stats(a: dict, b: dict) -> dict:
    """Sum tokens / duration / GPU time across retry call pairs. Keeps the
    cognitive_tasks cost accounting honest when validation forces a retry."""
    merged = dict(a) if a else {}
    if not b:
        return merged
    for k in ("tokens_in", "tokens_out", "duration_ms", "gpu_ms"):
        merged[k] = (merged.get(k) or 0) + (b.get(k) or 0)
    return merged


class CognitiveWorkerBase:
    """Subclasses MUST override name, default_model, num_ctx, max_runtime_s."""
    name: str = "base"
    default_model: str = ""
    upgrade_model: Optional[str] = None
    escalate_above_tokens: int = 4000
    num_ctx: int = 8192
    max_runtime_s: int = 120

    def pick_model(self, input_tokens: int) -> str:
        if self.upgrade_model and input_tokens > self.escalate_above_tokens:
            return self.upgrade_model
        return self.default_model

    async def call_text(
        self,
        config: dict,
        prompt: str,
        *,
        system: str = "",
        num_ctx: int | None = None,
        timeout_s: int | None = None,
        model: str | None = None,
    ) -> tuple[str | None, dict]:
        """Text completion with subscription-aware routing.

        Codex/Grok go through ``complete_subscription_chain``; legacy models
        keep the Ollama topic helper. Never uses ``llm_for`` for subscription.
        """
        from model_catalog import is_subscription_enabled

        m = model or self.default_model
        ctx = num_ctx if num_ctx is not None else self.num_ctx
        timeout = timeout_s if timeout_s is not None else self.max_runtime_s
        if is_subscription_enabled(m, config):
            from subscription_invoke import complete_text
            t = await complete_text(
                m, config=config, prompt=prompt, system=system or "", surface=self.name,
                timeout_s=timeout,
            )
            return t, {"model": m, "provider": "subscription"}
        from worker import _call_ollama_topic
        return await _call_ollama_topic(
            m, prompt, config, num_ctx=ctx, system=system, timeout_s=timeout,
        )

    async def call_and_parse(
        self,
        config: dict,
        prompt: str,
        result_type,
        *,
        system: str,
        num_ctx: int | None = None,
        timeout_s: int | None = None,
        initial_model: str | None = None,
        raise_on_empty: bool = True,
    ):
        """Call model, parse `result_type`. Subscription uses schema-aware complete_typed.

        Empty transport failures retry once. Validation failures retry with
        feedback (and upgrade_model when configured).
        Returns ``(parsed_or_None, merged_stats)``.
        """
        from agent_utils import parse_typed, validation_error_summary
        from model_catalog import is_subscription_enabled

        model = initial_model or self.default_model
        log = logging.getLogger(f"bernie.{self.name}")
        timeout = timeout_s if timeout_s is not None else self.max_runtime_s

        async def _run_once(m: str, p: str):
            if is_subscription_enabled(m, config):
                from subscription_invoke import complete_typed
                parsed = await complete_typed(
                    m,
                    result_type,
                    config=config,
                    prompt=p,
                    system=system,
                    surface=self.name,
                    timeout_s=timeout,
                )
                return parsed, {"model": m, "provider": "subscription"}
            text, stats = await self.call_text(
                config, p, system=system, num_ctx=num_ctx,
                timeout_s=timeout_s, model=m,
            )
            if not text:
                return None, stats
            return parse_typed(text, result_type), stats

        parsed, stats = await _run_once(model, prompt)

        if parsed is not None:
            return parsed, stats

        log.warning("%s: model returned no usable output; retrying once", self.name)
        parsed_retry, stats_retry = await _run_once(model, prompt)
        stats = merge_stats(stats, stats_retry)
        if parsed_retry is not None:
            return parsed_retry, stats

        # Validation-feedback / upgrade retry (Ollama free-text or failed typed).
        err = validation_error_summary("", result_type)
        retry_model = self.upgrade_model or model
        if self.upgrade_model:
            log.warning(
                "%s: validation failed (%s); retrying with upgrade_model=%s",
                self.name, err[:120], retry_model,
            )
        retry_prompt = (
            prompt
            + f"\n\nYour previous response failed validation: {err}\n"
            "Re-emit STRICT JSON matching the schema. No commentary, no markdown fences."
        )
        parsed2, stats2 = await _run_once(retry_model, retry_prompt)
        merged = merge_stats(stats, stats2)
        if parsed2 is not None:
            return parsed2, merged
        if raise_on_empty:
            raise RuntimeError(
                f"{self.name}: model returned no text after retry "
                f"(model={model}); failing the task so it surfaces in monitoring"
            )
        return None, merged

    async def handle(self, task: dict, _ctx) -> dict:
        """Subclass implements. Must return dict with keys _result and _stats."""
        raise NotImplementedError
