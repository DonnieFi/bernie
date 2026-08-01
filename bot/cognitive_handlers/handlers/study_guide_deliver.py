"""study_guide_deliver — email study guide + optional Discord pointer."""

from __future__ import annotations

import json as _json
import logging
from html import unescape as _unescape

from cognitive_handlers.registry import task_handler

log = logging.getLogger("bernie.worker")


@task_handler("study_guide_deliver")
async def handle_study_guide_deliver(task: dict, container) -> dict | None:
    _db = container.db
    payload = task.get("payload", {})
    if isinstance(payload, str):
        payload = _json.loads(payload or "{}")
    event_id = payload.get("event_id")
    person_id = (payload.get("person_id") or "").lower()
    summary = payload.get("summary") or "(study guide)"

    output = await _db.get_task_output_by_key(f"study_guide:{event_id}")
    if not output:
        log.warning("study_guide_deliver: no output for event_id=%s", event_id)
        return {"_result": {"delivered": False, "reason": "no_output"}, "_stats": {}}

    from config import config as cfg

    family = cfg.get("family_members", {}) or {}
    discord_id = None
    student_email = None
    if isinstance(family, dict):
        for display_name, p in family.items():
            if not isinstance(p, dict):
                continue
            cid = (p.get("canonical_id") or "").lower()
            fn = (p.get("first_name") or display_name).lower()
            if cid == person_id or fn == person_id:
                discord_id = p.get("discord_id")
                student_email = p.get("email")
                break

    if not student_email:
        log.warning("study_guide_deliver: no email for person_id=%s", person_id)
        return {"_result": {"delivered": False, "reason": "no_email"}, "_stats": {}}

    from email_service import resolve_family_cc_email, send

    cc_addr = resolve_family_cc_email(cfg, "study_guide_cc_email")

    try:
        subject = f"Prep for {_unescape(summary)[:100]}"
        body = _unescape(output["content"])
        msg_id = await send(
            student_email,
            subject,
            body,
            cc=cc_addr,
            requester_id="agent:study-guide-deliver",
            requester_role="system",
            config=cfg,
        )
    except Exception as e:
        log.exception("study_guide_deliver: email send failed for %s", student_email)
        return {"_result": {"delivered": False, "reason": f"email_failed: {e}"}, "_stats": {}}

    if discord_id:
        try:
            _router = container.notification_orchestrator if container else None
            if _router:
                await _router.notify(
                    _router.notification(
                        recipient_id=str(discord_id),
                        message=f"📧 Sent your study guide for **{_unescape(summary)}** to your email.",
                        urgency="normal",
                    )
                )
            else:
                raise RuntimeError("No router available")
        except Exception:
            try:
                from cross_container import post_to_discord

                await post_to_discord(
                    int(discord_id),
                    content=f"📧 Sent your study guide for **{_unescape(summary)}** to your email.",
                )
            except Exception:
                log.debug("study_guide_deliver: Discord pointer DM failed (non-fatal)", exc_info=True)

    return {
        "_result": {
            "delivered": True,
            "mode": "email",
            "to": student_email,
            "cc": cc_addr,
            "message_id": msg_id,
        },
        "_stats": {},
    }
