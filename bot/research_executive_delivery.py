"""Shared executive review + confidence routing for research delivery paths."""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass

from confidence_router import route_deliverable
from executive_review import review_deliverable
from typed_outputs import DeliverableMeta, ResearchDeliverable
import db_writes

log = logging.getLogger(__name__)


@dataclass
class ResearchDeliveryPlan:
    should_deliver: bool
    route: str
    body_text: str
    prefix: str
    urgency: str
    fallback: bool
    topic: str
    deliverable: ResearchDeliverable


async def prepare_research_for_delivery(
    content: str,
    topic: str,
    *,
    config: dict,
    container,
    db,
    requester_id: str | None = None,
    meta: DeliverableMeta | dict | None = None,
    source: str = "research",
) -> ResearchDeliveryPlan:
    """Review + route research output before any family-facing delivery."""
    if isinstance(meta, dict):
        try:
            meta_obj = DeliverableMeta.model_validate(meta)
        except Exception:
            meta_obj = DeliverableMeta()
    elif isinstance(meta, DeliverableMeta):
        meta_obj = meta
    else:
        meta_obj = DeliverableMeta()

    deliverable = ResearchDeliverable(
        topic=(topic or "research")[:200],
        content=content or "",
        meta=meta_obj,
    )
    reviewed = await review_deliverable(deliverable, config=config, container=container)
    fallback = reviewed is None
    if reviewed is not None:
        deliverable = reviewed
    elif deliverable.meta.draft_status == "draft":
        deliverable.meta.draft_status = "fallback"

    route = route_deliverable(deliverable.meta, config=config)

    if db is not None:
        try:
            await db_writes.routed("log_activity", 
                event_type="executive_review",
                description=f"{source} route={route} status={deliverable.meta.draft_status}",
                person_id=str(requester_id) if requester_id else None,
                meta=_json.dumps(
                    {
                        "route": route,
                        "confidence": deliverable.meta.confidence,
                        "fallback": fallback,
                        "source": source,
                    }
                ),
            )
        except Exception:
            log.debug("prepare_research_for_delivery: activity log failed", exc_info=True)

    prefix = "⚠️ " if fallback else ""
    urgency = "high" if route == "interrupt" else "normal"
    should_deliver = route not in ("ignore", "remember")

    return ResearchDeliveryPlan(
        should_deliver=should_deliver,
        route=route,
        body_text=deliverable.content,
        prefix=prefix,
        urgency=urgency,
        fallback=fallback,
        topic=deliverable.topic,
        deliverable=deliverable,
    )
