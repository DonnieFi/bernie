"""Discord delivery helpers for cognitive task handlers."""

from __future__ import annotations

import logging

log = logging.getLogger("bernie.worker")


async def deliver_discord_message(
    container,
    recipient_id: str | int,
    message: str,
    *,
    mention: str | None = None,
    urgency: str = "normal",
) -> bool:
    """Post to a channel or DM via NotificationRouter, then cross-container fallback."""
    rid = str(recipient_id)
    text = f"{mention}{message}" if mention else message
    router = container.notification_orchestrator if container else None
    if router:
        try:
            results = await router.notify(
                router.notification(recipient_id=rid, message=text, urgency=urgency)
            )
            if results.get("status") == "queued_quiet_hours":
                return True
            if results.get("discord"):
                return True
        except Exception:
            log.debug(
                "deliver_discord_message: router failed for %s, trying cross-container",
                rid,
                exc_info=True,
            )
    try:
        from cross_container import post_to_discord

        await post_to_discord(int(rid), content=text)
        return True
    except Exception:
        log.debug("deliver_discord_message: cross-container failed for %s", rid, exc_info=True)
        return False


async def deliver_discord_dm(container, recipient_id: str, message: str) -> bool:
    """DM via NotificationRouter when available; fall back to cross-container post."""
    return await deliver_discord_message(container, recipient_id, message)