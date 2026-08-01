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
        from model_catalog import is_subscription_enabled
        from subscription_invoke import complete_typed

        prompt = f"{_REVIEW_INSTRUCTION}\n\n{deliverable.model_dump_json()}"
        if is_subscription_enabled(audit_model, config):
            reviewed = await complete_typed(
                audit_model,
                ResearchDeliverable,
                config=config,
                prompt=prompt,
                surface="audit",
            )
            if reviewed is None:
                return None
        else:
            from agent_utils import make_typed_agent
            agent = make_typed_agent(audit_model, ResearchDeliverable, retries=1)
            result = await agent.run(prompt)
            reviewed = result.output
        if reviewed.meta.draft_status == "draft":
            reviewed.meta.draft_status = "reviewed"
        return reviewed
    except Exception:
        log.exception("review_deliverable failed (model=%s)", audit_model)
        return None
