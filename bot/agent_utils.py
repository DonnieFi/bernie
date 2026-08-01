"""Shared PydanticAI / Pydantic helpers for typed worker + judge output.

Two surfaces:

1. `make_typed_agent(model, result_type)` — PydanticAI Agent constructor used
   by frontier-model paths (judges, `claude-*`/LiteLLM-routed callers). Same
   routing as `call_for_audit`:
     claude-* → Anthropic direct (reads ANTHROPIC_API_KEY from env)
     else     → LiteLLM at litellm_base_url (config-driven, normalized to /v1)

2. `parse_typed(raw, result_type)` — Pydantic validation of free-text model
   output (used by Ollama-backed cognitive workers). Strips markdown fences,
   extracts balanced object/array blocks, validates. Returns instance | None
   so callers control retry/fallback. Phase 28.5 §2-§4 use this to replace
   the `re.search + json.loads + JSONDecodeError fallback` triplet across
   ReflectionWorker / MemoryConsolidationWorker / ResearchWorker.

The Agent surface stays separate from `parse_typed` because Ollama workers
need to preserve OLLAMA_SEMAPHORE + num_ctx control + langfuse trace shape,
which a bare `Agent.run()` over Ollama's OpenAI-compatible endpoint would
bypass. Decision recorded in 28-5-PLAN.md after investigating §2.

Pydantic AI 2.x (40B-1c): `output_type=` on Agent; `OpenAIChatModel` for
OpenAI-compat paths; `result.output` + `result.usage` (property) at call sites.
"""
from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pydantic import BaseModel
    from pydantic_ai import Agent as _Agent


def make_typed_agent(model: str, result_type: "type[BaseModel]", *, retries: int = 2) -> "_Agent":
    """Build a PydanticAI Agent for the given model + output type (v2).

    Preserves the exact three-tier routing used by judges and frontier workers:
      claude-* (or configured anthropic_models) → direct Anthropic
      ollama_models                              → Ollama via OpenAI-compat /v1
      everything else                            → LiteLLM (or OpenRouter) via /v1 shim

    A new agent is created per call — typed agents run at low frequency
    (nightly judges, per-worker tasks) so instance creation cost is
    negligible vs API latency.
    """
    from pydantic_ai import Agent
    try:
        from config import config as _cfg
    except Exception:
        log.warning("make_typed_agent: config read failed, using defaults", exc_info=True)
        _cfg = {}

    from model_registry import model_source
    source = model_source(model, _cfg)

    if source == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            log.warning("make_typed_agent: ANTHROPIC_API_KEY unset; Anthropic call will fail")
            api_key = "no-key"
        m = AnthropicModel(model, provider=AnthropicProvider(api_key=api_key))
    elif source == "ollama":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        from ollama_resolver import current_ollama_base_url
        ollama_raw = current_ollama_base_url(_cfg)
        ollama_url = ollama_raw.rstrip("/") + "/v1"
        m = OpenAIChatModel(model, provider=OpenAIProvider(base_url=ollama_url, api_key="ollama"))
    else:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        raw = _cfg.get("litellm_base_url", "https://litellm.example.local")
        base_url = raw.rstrip("/") + "/v1"
        api_key = os.environ.get("LTE_LLM_MASTER_KEY", "")
        if not api_key:
            log.warning("make_typed_agent: LTE_LLM_MASTER_KEY unset; LiteLLM call will 401")
            api_key = "no-key"
        profile = None
        try:
            from pydantic_ai.profiles.openai import OpenAIModelProfile
            profile = OpenAIModelProfile(openai_chat_supports_max_completion_tokens=False)
        except ImportError:
            pass
        m = OpenAIChatModel(
            model,
            provider=OpenAIProvider(base_url=base_url, api_key=api_key),
            profile=profile,
        )

    return Agent(m, output_type=result_type, retries=retries)


def usage_token_counts(usage) -> tuple[int, int]:
    """Normalize RunUsage across v0 mocks and Pydantic AI v2 RunUsage."""
    if callable(usage):
        usage = usage()
    inp = getattr(usage, "input_tokens", None) or getattr(usage, "request_tokens", 0) or 0
    out = getattr(usage, "output_tokens", None) or getattr(usage, "response_tokens", 0) or 0
    return int(inp), int(out)


def parse_typed(raw: str, result_type: "type[BaseModel]"):
    """Validate raw model output against `result_type`. Returns the typed
    instance on success, None on failure.

    Tries three candidates in order:
      1. The raw string as-is (most common — small Ollama models with strict
         JSON system prompts get this right).
      2. After stripping ``` / ```json markdown fences.
      3. The outermost balanced `{...}` block (or `[...]` for list-typed
         results), in case the model emitted preamble or trailing prose.

    All ValidationError / ValueError / JSONDecodeError exceptions are caught;
    the caller decides whether to retry, fall back to a default instance, or
    skip entirely. No silent fail-open inside this function — None is an
    explicit failure signal.
    """
    if not raw or not isinstance(raw, str):
        return None
    from pydantic import ValidationError

    stripped = raw.strip()
    candidates: list[str] = [stripped]

    fenced = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", stripped, flags=re.MULTILINE).strip()
    if fenced and fenced != stripped:
        candidates.append(fenced)

    # Outermost balanced { } or [ ] scan — handles chatty preamble / trailing prose.
    for opening, closing in (("{", "}"), ("[", "]")):
        start = stripped.find(opening)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(stripped)):
            ch = stripped[i]
            if ch == opening:
                depth += 1
            elif ch == closing:
                depth -= 1
                if depth == 0:
                    block = stripped[start:i + 1]
                    if block not in candidates:
                        candidates.append(block)
                    break

    for cand in candidates:
        try:
            return result_type.model_validate_json(cand)
        except (ValidationError, ValueError):
            continue
    return None


def validation_error_summary(raw: str, result_type: "type[BaseModel]") -> str:
    """Best-effort short error message describing why parse_typed returned None.

    Use to build retry prompts or to enrich warning logs. Caps output at
    300 chars so it doesn't blow up the prompt context.
    """
    if not raw:
        return "no output"
    from pydantic import ValidationError
    try:
        result_type.model_validate_json((raw or "").strip())
    except ValidationError as e:
        errors = e.errors()
        if errors:
            first = errors[0]
            loc = ".".join(str(x) for x in first.get("loc", ()))
            msg = first.get("msg", "?")
            return f"{loc}: {msg}"[:300]
        return str(e)[:300]
    except ValueError as e:
        return str(e)[:300]
    # Top-level validation passed here; parse_typed shouldn't have returned
    # None in that case (its first candidate is the same stripped raw). If
    # this string surfaces, it's a caller-side logic error, not bad output.
    return "validation passed on retry — caller logic error?"
