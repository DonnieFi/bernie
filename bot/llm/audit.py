"""Audit synthesis logic (Phase 4.4 Session 3).

Moved from claude_service.py.
"""
import logging

from .observability import log_llm_turn
from .clients import llm_client_is_ephemeral, close_client

log = logging.getLogger(__name__)

AUDIT_SYSTEM_PROMPT = (
    "You are Bernie, the Example household assistant. "
    "You are reviewing a nightly technical audit of home infrastructure. "
    "Write a calm, clear executive summary. Focus on actionable failures. "
    "Ignore routine noise. Plain prose only — this will be emailed.\n\n"
    "Base your summary ONLY on the data provided below. Report exactly what the "
    "data shows. If a section reports no data, or says a check was unavailable or "
    "skipped, state that plainly — do NOT infer an outage from missing data. "
    "Never tell the reader to check their home network or internet connection, or "
    "to power-cycle any hardware or monitoring system, unless a remote-health "
    "entity in the data is explicitly reported offline. You are summarising a "
    "report, not diagnosing the reader's house."
)

async def call_for_audit(draft: str, cfg: dict, container) -> str:
    """Minimal single-turn Claude call for Watchman executive synthesis.

    Deliberately bypasses the full chat pipeline so nightly audits don't
    appear in conversation history, inflate usage stats under a fake persona,
    or pull in memory/calendar context that isn't relevant to a log review.
    """
    from llm.ollama import call_ollama
    
    audit_model = cfg.get("audit_model")
    if not audit_model:
        log.error("call_for_audit: audit_model is not configured; returning draft")
        return draft
    client_or_url = container.llm_for(audit_model)
    if isinstance(client_or_url, str):
        # Ollama watchman fallback — avoids 404 on the raw client
        return await call_ollama(
            AUDIT_SYSTEM_PROMPT,
            [{"role": "user", "content": draft}],
            cfg, None, model_override=audit_model
        )
    client = client_or_url
    try:
        resp = await client.messages.create(
            model=audit_model,
            max_tokens=400,
            system=AUDIT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": draft}],
        )
        text = resp.content[0].text
        _cc_tok = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
        _cr_tok = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        
        await log_llm_turn(
            model=audit_model,
            user_input=draft,
            output=text,
            input_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(resp.usage, "output_tokens", 0) or 0,
            cache_creation_tokens=_cc_tok,
            cache_read_tokens=_cr_tok,
            triggered_by="scheduler",
            name="audit_synthesis"
        )
        return text
    except Exception as e:
        log.error("call_for_audit: synthesis failed (model=%s): %s", audit_model, e)
        return draft  # Fall back to raw Ollama draft on failure
    finally:
        if llm_client_is_ephemeral(client, container):
            await close_client(client, container)
