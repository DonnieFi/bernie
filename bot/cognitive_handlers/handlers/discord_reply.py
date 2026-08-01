"""discord_reply cognitive task — background topic → Discord post."""

from __future__ import annotations

import logging

from cognitive_handlers.delivery import deliver_discord_message
from cognitive_handlers.registry import task_handler
from cognitive_handlers.worker_shared import call_worker_model

log = logging.getLogger("bernie.worker")


@task_handler("discord_reply")
async def handle_discord_reply(task: dict, container) -> dict | None:
    payload = task.get("payload", {})
    topic = payload.get("topic", "")
    channel_id = payload.get("channel_id") or task.get("channel_id")
    actor_id = payload.get("actor_id") or task.get("actor_id", "")

    if not topic or not channel_id:
        log.warning("discord_reply task missing topic or channel_id: %s", task.get("id"))
        return None

    answer = await call_worker_model(topic)
    if not answer:
        raise RuntimeError("No answer from Anthropic for deferred topic")

    mention = f"<@{actor_id}> " if actor_id and str(actor_id) != "0" else None
    if not await deliver_discord_message(
        container,
        channel_id,
        answer,
        mention=mention,
    ):
        raise RuntimeError(f"Failed to post discord reply to {channel_id}")

    log.info("discord_reply: sent to %s", channel_id)
    return {"posted": True, "channel_id": channel_id}