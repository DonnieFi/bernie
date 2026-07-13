"""research_deliver — DM or email completed research output."""

from __future__ import annotations

import json as _json
import logging

from cognitive_handlers.delivery import deliver_discord_dm
from cognitive_handlers.registry import task_handler
from delivery_gateway import send_email_via_gateway
from research_executive_delivery import prepare_research_for_delivery

log = logging.getLogger("bernie.worker")


async def _deliver_dm_chunks(
    container,
    requester_id: str,
    chunks: list[str],
    *,
    urgency: str = "normal",
) -> tuple[bool, int]:
    """Send DM chunks via NotificationRouter when urgency is set."""
    router = container.notification_orchestrator if container else None
    sent = 0
    for chunk in chunks:
        if router:
            try:
                results = await router.notify(
                    router.notification(
                        recipient_id=str(requester_id),
                        message=chunk,
                        urgency=urgency,
                    )
                )
                if results.get("status") == "queued_quiet_hours" or results.get("discord"):
                    sent += 1
                    continue
            except Exception:
                log.debug("research_deliver: router chunk failed, trying helper", exc_info=True)
        if await deliver_discord_dm(container, requester_id, chunk):
            sent += 1
        else:
            return False, sent
    return True, sent


@task_handler("research_deliver")
async def handle_research_deliver(task: dict, container) -> dict | None:
    _db = container.db
    payload = task.get("payload", {})
    if isinstance(payload, str):
        payload = _json.loads(payload or "{}")
    src_task_id = payload.get("task_id")
    requester_id = payload.get("requester_id")
    topic = payload.get("topic") or "research"
    delivery = (payload.get("delivery") or "dm").lower()
    email = (payload.get("email") or "").strip()

    if src_task_id is not None:
        try:
            parent = await _db.get_cognitive_task(int(src_task_id))
            if parent:
                parent_payload = parent.get("payload") or {}
                delivery = (parent_payload.get("delivery") or delivery).lower()
                email = (parent_payload.get("email") or email).strip()
        except Exception:
            pass
    if delivery not in ("dm", "email"):
        delivery = "dm"

    out = await _db.get_task_output_by_key(f"research:{src_task_id}")
    if not out:
        return {"_result": {"delivered": False, "reason": "no_output"}, "_stats": {}}

    from config import config as cfg

    meta_payload = payload.get("meta")
    plan = await prepare_research_for_delivery(
        out["content"],
        topic,
        config=cfg,
        container=container,
        db=_db,
        requester_id=str(requester_id) if requester_id else None,
        meta=meta_payload if isinstance(meta_payload, dict) else None,
        source="research_deliver",
    )

    if not plan.should_deliver:
        reason = "ignored" if plan.route == "ignore" else "remember_only"
        return {
            "_result": {"delivered": False, "reason": reason, "route": plan.route},
            "_stats": {},
        }

    prefix = plan.prefix
    body_text = plan.body_text
    from email_service import resolve_family_cc_email

    cc_addr = resolve_family_cc_email(cfg, "research_cc_email")
    urgency = plan.urgency

    if delivery == "email":
        if not email:
            return {"_result": {"delivered": False, "reason": "no_email"}, "_stats": {}}
        subject = f"{prefix}Research: {topic[:120]}"
        body = f"{body_text}\n\n— Bernie"
        try:
            send_result = await send_email_via_gateway(
                to=email,
                subject=subject,
                body=body,
                cc=cc_addr,
                config=cfg,
                container=container,
            )
            return {
                "_result": {
                    "delivered": True,
                    "mode": "email",
                    "route": plan.route,
                    "to": email,
                    "cc": cc_addr,
                    "send_result": (send_result or "")[:200],
                },
                "_stats": {},
            }
        except Exception as e:
            log.warning("research_deliver: email to %s failed: %s", email, e)
            # Fall back to DM when email fails so research is not lost.
            if requester_id:
                body = f"{prefix}**Research: {topic}**\n\n{body_text}"
                ok, sent = await _deliver_dm_chunks(
                    container, requester_id, [body], urgency=urgency
                )
                if ok:
                    return {
                        "_result": {
                            "delivered": True,
                            "mode": "dm_fallback",
                            "route": plan.route,
                            "email_error": str(e)[:200],
                            "chunks": sent,
                        },
                        "_stats": {},
                    }
            return {"_result": {"delivered": False, "reason": f"email_failed: {e}"}, "_stats": {}}

    body = f"{prefix}**Research: {topic}**\n\n{body_text}"
    # NotificationRouter DMs use send_chunked; cap at ~5 chunks (9500 chars) for UX.
    _DM_BODY_CAP = 9500

    try:
        await send_email_via_gateway(
            to=cc_addr,
            subject=f"{prefix}Research (DM copy): {topic[:120]}",
            body=f"{body_text}\n\n— Bernie (also DM'd to requester {requester_id})",
            config=cfg,
            container=container,
        )
    except Exception:
        log.debug("research_deliver: cc-copy email failed (non-fatal)", exc_info=True)

    if len(body) > _DM_BODY_CAP:
        notice = (
            f"{prefix}**Research: {topic}**\n\nResult was too long for chat ({len(body)} chars). "
            f"Saved to task #{src_task_id}. Full copy emailed to {cc_addr}."
        )
        ok, _ = await _deliver_dm_chunks(container, requester_id, [notice], urgency=urgency)
        if not ok:
            return {"_result": {"delivered": False, "reason": "dm_failed"}, "_stats": {}}
        return {
            "_result": {
                "delivered": True,
                "mode": "summary_only",
                "route": plan.route,
                "length": len(body),
                "cc": cc_addr,
            },
            "_stats": {},
        }

    ok, sent = await _deliver_dm_chunks(container, requester_id, [body], urgency=urgency)
    if not ok:
        return {
            "_result": {
                "delivered": False,
                "reason": "dm_failed",
                "route": plan.route,
                "chunks_sent": sent,
                "chunks_total": 1,
            },
            "_stats": {},
        }
    return {
        "_result": {
            "delivered": True,
            "mode": "dm",
            "route": plan.route,
            "chunks": 1,
            "cc": cc_addr,
        },
        "_stats": {},
    }
