"""
Judge scoring (Phase 4.3 Session 2 carve from eval_service.py).

Public: judge_pair, judge_triplet
Internal: _make_judge_agent, _judge_fallback_chain, _run_judge_with_fallbacks, _is_transient_judge_error

Exact copy of logic (no rewrites). Shared consts (ANTHROPIC_KEY, DEFAULT_JUDGE_MODEL)
imported from eval._http . DB access via get_database() / injected paths (no raw
`import database`). PydanticAI path via agent_utils.make_typed_agent (per CLAUDE.md:
structured output for nightly judges).

eval_service.py remains thin facade re-exporting these names so existing
imports, patches ("eval_service.judge_pair"), and call sites (nightly_eval_worker,
scripts/run_judge_backfill.py, tests) are unchanged.

Do not move HITL/nightly/audit/shadow code.
"""
import logging

from telemetry import fire_and_forget
from db_binding import get_database
import db_writes

log = logging.getLogger(__name__)


def _eval_service():
    """Lazy facade lookup so tests can patch eval_service.* and we avoid a load-time cycle."""
    import eval_service
    return eval_service


# ── Judge ─────────────────────────────────────────────────────────────────────

def _make_judge_agent(eval_model: str, result_type: "type[BaseModel]"):
    """Thin wrapper around agent_utils.make_typed_agent.

    Phase 28.5 extracted the routing into bot/agent_utils.py so cognitive
    workers can build typed agents without importing eval_service. The
    judge_pair / judge_triplet call sites keep working unchanged through
    this wrapper.
    """
    from agent_utils import make_typed_agent
    return make_typed_agent(eval_model, result_type)


def _judge_fallback_chain(primary_model: str) -> list[str]:
    """Build the [primary, litellm, ollama] tier list for judges.

    Tier 2 + 3 come from `config["eval"]`:
      - `judge_fallback_model`     — LiteLLM-routed (e.g. "or-deepseek-v4")
      - `judge_ollama_fallback`    — Ollama-routed   (e.g. "hermes3:8b-llama3.1-q6_K")
    Duplicates and empties are dropped, primary always leads.
    """
    try:
        from config import config as _cfg
    except Exception:
        _cfg = {}
    eval_cfg = _cfg.get("eval") or {}
    chain = [primary_model]
    for key in ("judge_fallback_model", "judge_ollama_fallback"):
        m = eval_cfg.get(key)
        if m and m not in chain:
            chain.append(m)
    return chain


def _is_transient_judge_error(exc: BaseException) -> bool:
    """True when the judge tier failed for an upstream reason we should retry on a new tier.

    Includes pydantic_ai ModelHTTPError 5xx/429, network timeouts, and the
    full surface of transport-layer exceptions (httpx ConnectError /
    ReadError, anthropic.APIConnectionError, aiohttp ClientConnectorError,
    bare ConnectionError, etc.). Validation errors (which surface as
    UnexpectedModelBehavior after retries=2) are NOT transient — there's no
    point sending the same prompt to a smaller model expecting better JSON;
    we let those bubble up as None per the original judge contract.

    The 2026-05-21 Anthropic outage surfaced as APIConnectionError, which
    the original class-name-equality check missed; that's why this now uses
    a substring match against ('connect', 'timeout', 'network').
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        # 401/403 advance because each tier has independent auth (Anthropic
        # key, LiteLLM master key, no-auth Ollama). A broken key on one tier
        # is not a signal that the prompt itself is bad.
        if status in (401, 403, 408, 429) or status >= 500:
            return True
        # 400/404/422 indicate a prompt/route error that would repeat on any
        # tier — let those surface as a hard failure.
        return False
    name = exc.__class__.__name__.lower()
    if any(tok in name for tok in ("connect", "timeout", "network")):
        return True
    # Belt around the substring match: keep the explicit pydantic_ai /
    # anthropic / aiohttp class-name allowlist. None of these names contain
    # the transport keywords above (ModelHTTPError → "modelhttperror",
    # APIError → "apierror", ClientError → "clienterror") so they would be
    # missed otherwise.
    if exc.__class__.__name__ in {"ModelHTTPError", "ModelAPIError", "APIError", "ClientError"}:
        return True
    return False


async def _run_judge_with_fallbacks(prompt: str, result_type, primary_model: str, label: str):
    """Run the judge across the [primary → LiteLLM → Ollama] chain.

    Returns `(result, model_used)` on success or raises the last exception
    if every tier failed. Callers wrap in their own try/except to preserve
    the legacy "return None on any error" contract.
    """
    chain = _judge_fallback_chain(primary_model)
    last_exc: BaseException | None = None
    for idx, model in enumerate(chain):
        try:
            agent = _eval_service()._make_judge_agent(model, result_type)
            result = await agent.run(prompt)
            if idx > 0:
                log.warning(
                    "%s recovered via tier-%d fallback model=%s (primary=%s failed)",
                    label, idx + 1, model, primary_model,
                )
            return result, model
        except Exception as e:
            last_exc = e
            if idx + 1 < len(chain) and _is_transient_judge_error(e):
                log.warning(
                    "%s tier-%d (%s) transient failure %r; advancing to tier-%d",
                    label, idx + 1, model, e, idx + 2,
                )
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{label}: no fallback tiers configured")


async def judge_pair(
    primary_response: str,
    shadow_response: str,
    eval_model: str | None = None,
    user_message: str | None = None,
) -> dict | None:
    """Score both responses via judge model. Returns dict with 4 floats or None.

    Scores:
      *_intent:  Did it correctly answer what the user asked? (0–1)
      *_tool:    Factual grounding — are claims verifiable / free of hallucination? (0–1)
                 (repurposed from the old tool-accuracy dimension, which was always 1.0
                  for shadow calls since they never invoke tools)

    Uses PydanticAI structured output with auto-retry on validation failure,
    replacing the previous regex + json.loads approach that silently dropped
    ~1-2 scores per nightly run when the model added prose around the JSON.
    """
    svc = _eval_service()
    judge_model = eval_model or svc.DEFAULT_JUDGE_MODEL
    if judge_model.startswith("claude-") and not svc.ANTHROPIC_KEY:
        return None

    context_block = f"User asked: {user_message[:300]}\n\n" if user_message else ""
    prompt = f"""You are an impartial judge evaluating two AI assistant responses for a family home-assistant bot.

{context_block}Response A: {primary_response[:1000]}

Response B: {shadow_response[:1000]}

Score each response on TWO dimensions (0.0 to 1.0):

1. a_intent / b_intent — Does the response correctly and helpfully answer what the user asked?
   - 1.0: directly answers, accurate, helpful
   - 0.5: partially answers or slightly off-topic
   - 0.0: ignores the question or gives a wrong answer

2. a_factual / b_factual — Are the specific claims, names, locations, URLs, and data verifiable and plausible?
   - 1.0: all claims are grounded; no invented data
   - 0.5: some unverifiable claims or vague assertions
   - 0.0: fabricated URLs, invented locations, made-up names, impossible facts

IMPORTANT: A confident-sounding response with invented details scores LOW on factual grounding."""

    try:
        from eval_models import JudgePairResult
        result, model_used = await _run_judge_with_fallbacks(
            prompt, JudgePairResult, judge_model, "judge_pair",
        )
        from agent_utils import usage_token_counts
        scores: JudgePairResult = result.output
        try:
            input_tokens, output_tokens = usage_token_counts(result.usage)
            from langfuse_logger import log_generation
            fire_and_forget(log_generation(
                model=model_used,
                user_input=prompt,
                output=scores.model_dump_json(),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                name="judge_pair",
                triggered_by="eval:judge_pair",
            ))
            # Also record in token_usage for unified cost dashboards
            try:
                fire_and_forget(db_writes.routed("log_token_usage",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    model=model_used,
                    triggered_by="eval:judge_pair",
                ))
            except Exception:
                pass
        except Exception:
            log.debug("langfuse judge_pair trace failed (non-fatal)", exc_info=True)
        return {
            "primary_intent": scores.a_intent,
            "primary_tool":   scores.a_factual,
            "shadow_intent":  scores.b_intent,
            "shadow_tool":    scores.b_factual,
        }
    except Exception:
        log.exception("judge_pair failed across all fallback tiers")
        return None


async def judge_triplet(row: dict, eval_model: str) -> dict | None:
    """Score a three-call shadow triplet via LLM judge.

    Returns dict with keys 'winner', 'A', 'B', 'C' (each with intent_match,
    tool_accuracy, preference scores 0-10), and 'reasoning'. Returns None on
    failure so nightly_eval_worker can skip and move on.

    Uses PydanticAI structured output — winner is validated as Literal["A","B","C","none"]
    so the previous silent failure mode (model returning "Primary" or "Response A")
    now retries instead of being silently dropped. The old fence-stripping + json.loads
    path that left a "json\\n" prefix and failed silently is also gone.
    """
    svc = _eval_service()
    eval_model = eval_model or svc.DEFAULT_JUDGE_MODEL

    primary = row.get("primary_response", "") or ""
    model_shadow = row.get("shadow_response", "") or ""
    harness_shadow = row.get("harness_shadow_response", "") or ""
    user_msg = row.get("user_message", "") or ""

    if not any([primary, model_shadow, harness_shadow]):
        return None

    if eval_model.startswith("claude-") and not svc.ANTHROPIC_KEY:
        return {"winner": None, "reasoning": "ANTHROPIC_API_KEY not set"}

    prompt = f"""You are an impartial judge evaluating three AI responses to the same message.

User message: {user_msg[:500]}

Response A (primary): {primary[:600]}

Response B (model shadow): {model_shadow[:600]}

Response C (harness shadow): {harness_shadow[:600]}

Score each response on:
- intent_match (0-10): Does it correctly understand and address the user's intent?
- tool_accuracy (0-10): Did it use the right tools / take the right actions?
- preference (0-10): Overall helpfulness and quality.

Set winner to "A", "B", "C", or "none" if all are equal.
Provide one sentence of reasoning."""

    try:
        from eval_models import JudgeTripletResult
        result, model_used = await _run_judge_with_fallbacks(
            prompt, JudgeTripletResult, eval_model, "judge_triplet",
        )
        from agent_utils import usage_token_counts
        scores: JudgeTripletResult = result.output
        try:
            input_tokens, output_tokens = usage_token_counts(result.usage)
            from langfuse_logger import log_generation
            fire_and_forget(log_generation(
                model=model_used,
                user_input=prompt,
                output=scores.model_dump_json(),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                name="judge_triplet",
                triggered_by="eval:judge_triplet",
            ))
            # DB row for unified dashboards
            try:
                fire_and_forget(db_writes.routed("log_token_usage",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    model=model_used,
                    triggered_by="eval:judge_triplet",
                ))
            except Exception:
                pass
        except Exception:
            log.debug("langfuse judge_triplet trace failed (non-fatal)", exc_info=True)
        return {
            "winner": scores.winner,
            "reasoning": scores.reasoning,
            "A": {"intent_match": scores.A.intent_match, "tool_accuracy": scores.A.tool_accuracy, "preference": scores.A.preference},
            "B": {"intent_match": scores.B.intent_match, "tool_accuracy": scores.B.tool_accuracy, "preference": scores.B.preference},
            "C": {"intent_match": scores.C.intent_match, "tool_accuracy": scores.C.tool_accuracy, "preference": scores.C.preference},
        }
    except Exception:
        log.exception("judge_triplet failed across all fallback tiers")
        return None
