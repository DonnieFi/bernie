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
        """Call Ollama, parse `result_type` from the response. Two failure
        modes are handled differently:

        - **Empty text** (Ollama timed out, dropped socket, returned no body)
          is treated as a transient transport failure. We retry once on the
          same model. If still empty and ``raise_on_empty`` is True (the
          default), we raise RuntimeError so the worker's ``handle()`` fails
          loudly — Watchman then sees the cognitive_task marked failed
          rather than a healthy 0-row pass masking a broken upstream.
          Research-style workers that legitimately tolerate empty
          intermediate calls (e.g. the query-planner step deciding it has
          enough information) pass ``raise_on_empty=False`` to get
          ``(None, stats)`` instead of an exception.

        - **Validation failure** (text came back but doesn't parse) means
          the model is misbehaving on shape. We retry once with the
          validation error appended to the prompt, escalating to
          ``upgrade_model`` when one is configured — a small model that
          emitted unparseable JSON once usually emits the same garbage on
          the second try, so switching tier is the cheap recovery path.
          On exhaustion we return ``(None, stats)`` so the caller can
          substitute an empty default.

        Returns ``(parsed_or_None, merged_stats)``.
        """
        from worker import _call_ollama_topic
        from agent_utils import parse_typed, validation_error_summary

        model = initial_model or self.default_model
        ctx = num_ctx if num_ctx is not None else self.num_ctx
        timeout = timeout_s if timeout_s is not None else self.max_runtime_s
        log = logging.getLogger(f"bernie.{self.name}")

        text, stats = await _call_ollama_topic(
            model, prompt, config,
            num_ctx=ctx, system=system, timeout_s=timeout,
        )

        if not text:
            log.warning("%s: Ollama returned no text; retrying once (transient)", self.name)
            text_retry, stats_retry = await _call_ollama_topic(
                model, prompt, config,
                num_ctx=ctx, system=system, timeout_s=timeout,
            )
            stats = merge_stats(stats, stats_retry)
            if not text_retry:
                if raise_on_empty:
                    raise RuntimeError(
                        f"{self.name}: Ollama returned no text after retry "
                        f"(model={model}); failing the task so it surfaces in monitoring"
                    )
                return None, stats
            text = text_retry

        parsed = parse_typed(text, result_type)
        if parsed is not None:
            return parsed, stats

        err = validation_error_summary(text, result_type)
        retry_model = self.upgrade_model or model
        if self.upgrade_model:
            log.warning(
                "%s: validation failed (%s); retrying with upgrade_model=%s",
                self.name, err[:120], retry_model,
            )
        else:
            log.warning(
                "%s: validation failed (%s); retrying same model with feedback",
                self.name, err[:120],
            )
        retry_prompt = (
            prompt
            + f"\n\nYour previous response failed validation: {err}\n"
            "Re-emit STRICT JSON matching the schema. No commentary, no markdown fences."
        )
        text2, stats2 = await _call_ollama_topic(
            retry_model, retry_prompt, config,
            num_ctx=ctx, system=system, timeout_s=timeout,
        )
        merged = merge_stats(stats, stats2)
        if not text2:
            return None, merged
        return parse_typed(text2, result_type), merged

    async def handle(self, task: dict, _ctx) -> dict:
        """Subclass implements. Must return dict with keys _result and _stats."""
        raise NotImplementedError
