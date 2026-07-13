"""Executive review for typed family deliverables (Phase 29 Wave E)."""

from __future__ import annotations

import logging

from typed_outputs import ResearchDeliverable

log = logging.getLogger(__name__)

_REVIEW_INSTRUCTION = (
    "You are Bernie's executive reviewer. Given a research deliverable JSON, return the "
    "same schema. Set meta.draft_status to 'reviewed'. Adjust meta.confidence, urgency, "
    "and impact to reflect quality and family relevance. Do not invent facts; keep content "
    "unchanged unless trimming unsafe speculation."
)


async def review_deliverable(
    deliverable: ResearchDeliverable,
    *,
    config: dict,
    container,
) -> ResearchDeliverable | None:
    """Frontier-model review; None → caller uses fallback prefix on draft."""
    audit_model = config.get("audit_model")
    if not audit_model:
        log.warning("review_deliverable: audit_model unset — skipping review")
        return None
    try:
        from agent_utils import make_typed_agent

        agent = make_typed_agent(audit_model, ResearchDeliverable, retries=1)
        prompt = f"{_REVIEW_INSTRUCTION}\n\n{deliverable.model_dump_json()}"
        result = await agent.run(prompt)
        reviewed = result.output
        if reviewed.meta.draft_status == "draft":
            reviewed.meta.draft_status = "reviewed"
        return reviewed
    except Exception:
        log.exception("review_deliverable failed (model=%s)", audit_model)
        return None
