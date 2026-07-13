"""Post kid-initiated email drafts to #smithy for parent approval."""

from __future__ import annotations

import json
import logging
import db_writes

log = logging.getLogger(__name__)


async def post_email_pending_approval(
    *,
    pending_id: int,
    pending_row: dict,
    config: dict,
    router,
) -> int | None:
    """Post draft to approval channel; store mapping + reactions. Returns message id."""
    channel_id = (
        (config.get("email") or {}).get("approval_channel_id")
        or config.get("schedule_channel_id")
    )
    if not channel_id or router is None:
        log.warning("email_pending: no approval channel or router")
        return None

    cc_list = pending_row.get("cc") or "[]"
    if isinstance(cc_list, str):
        try:
            cc_list = json.loads(cc_list)
        except Exception:
            cc_list = []

    cc_line = f"\n**CC:** {', '.join(cc_list)}" if cc_list else ""
    text = (
        f"📧 **Email approval** — {pending_row.get('requester_id')} wants to send:\n"
        f"**To:** {pending_row.get('recipient')}{cc_line}\n"
        f"**Subject:** {pending_row.get('subject')}\n\n"
        f"{(pending_row.get('body') or '')[:1500]}\n\n"
        "Parents: react ✅ to send or ❌ to cancel."
    )
    event_id = f"email_pending:{pending_id}"

    try:
        results = await router.notify(
            router.notification(
                recipient_id=str(channel_id),
                message=text,
                urgency="high",
                event_id=event_id,
                message_type="email_pending",
            )
        )
        msg = results.get("discord")
        if msg and hasattr(msg, "id"):
            from db_binding import get_database

            db = get_database()
            await db_writes.routed("update_email_pending_smithy_message", pending_id, msg.id)
            await db_writes.routed("store_message_mapping", 
                msg.id,
                event_id,
                pending_row.get("subject", "")[:200],
                message_type="email_pending",
            )
            try:
                await msg.add_reaction("✅")
                await msg.add_reaction("❌")
            except Exception:
                log.debug("email_pending: reaction add failed", exc_info=True)
            return msg.id
    except Exception:
        log.exception("email_pending: post failed for id=%s", pending_id)
    return None


async def _notify_kid_expired(config: dict, router, row: dict) -> None:
    if router is None:
        return
    requester_id = (row.get("requester_id") or "").strip()
    recipient = row.get("recipient") or "recipient"
    if not requester_id:
        return
    try:
        from constants import registry as person_registry

        person = person_registry.get(requester_id) or person_registry.get(
            person_registry.resolve(requester_id) or ""
        )
        discord_id = (person or {}).get("discord_id")
        if not discord_id:
            return
        await router.notify(
            router.notification(
                recipient_id=str(discord_id),
                message=(
                    f"⏰ Your email to **{recipient}** was not approved within 24 hours "
                    "and has expired — it was not sent."
                ),
                urgency="normal",
            )
        )
    except Exception:
        log.debug("email_pending expiry: kid notify failed for %s", requester_id, exc_info=True)


async def run_email_pending_expiry_sweep(config: dict, bot) -> int:
    """Expire kid email drafts older than 24h; edit #smithy message when possible."""
    from datetime import datetime, timedelta, timezone as dt_timezone

    from db_binding import get_database
    from llm.model_state import get_container

    db = get_database()
    container = get_container()
    router = container.notification_orchestrator if container else None
    cutoff = (datetime.now(dt_timezone.utc) - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    rows = await db_writes.routed("expire_stale_email_pending", cutoff)
    for row in rows:
        await db_writes.routed("log_activity", 
            event_type="email_pending_expired",
            description=f"Pending email #{row.get('id')} expired",
            person_id="system:expiry",
        )
        await _notify_kid_expired(config, router, row)
        msg_id = row.get("smithy_message_id")
        if not msg_id or bot is None:
            continue
        try:
            ch_id = (config.get("email") or {}).get("approval_channel_id") or config.get("schedule_channel_id")
            channel = bot.get_channel(int(ch_id)) if ch_id else None
            if channel is None and ch_id:
                channel = await bot.fetch_channel(int(ch_id))
            if channel:
                msg = await channel.fetch_message(int(msg_id))
                await msg.edit(content=(msg.content or "") + "\n\n⏰ **Expired — not sent.**")
                try:
                    await msg.clear_reactions()
                except Exception:
                    log.debug("email_pending expiry: clear_reactions failed", exc_info=True)
        except Exception:
            log.debug("email_pending expiry: could not edit message %s", msg_id, exc_info=True)
    return len(rows)