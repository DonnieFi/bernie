"""Bernie-discord internal HTTP API (Wave 2b cross-container posting)."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import database
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class InternalPostPayload(BaseModel):
    channel_id: int
    content: Optional[str] = None
    embed: Optional[dict[str, Any]] = None
    reference_message_id: Optional[int] = None
    reactions: Optional[list[str]] = None


class InternalHitlNotifyPayload(BaseModel):
    pending_id: int


class InternalReactPayload(BaseModel):
    channel_id: int
    message_id: int
    emoji: str


async def resolve_discord_channel(bot, channel_id: int):
    """Resolve guild channel, or open DM when channel_id is a user snowflake.

    DM fallback only on NotFound/404 (family-bot-05u). Forbidden and other
    channel errors are not masked as DM attempts.
    """
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except Exception as ch_exc:
        err_name = type(ch_exc).__name__
        status = getattr(ch_exc, "status", None)
        is_not_found = err_name == "NotFound" or status == 404
        if not is_not_found:
            raise
        try:
            user = bot.get_user(channel_id)
            if user is None:
                user = await bot.fetch_user(channel_id)
            return user.dm_channel or await user.create_dm()
        except Exception as user_exc:
            raise ch_exc from user_exc


def create_internal_discord_app(bot):
    """FastAPI app for /internal/post, /internal/react, /internal/hitl/notify."""
    from fastapi import Body, FastAPI, Header, HTTPException

    internal_app = FastAPI(title="Bernie Internal (Wave 2b)")

    def _check_internal_auth(x_internal_auth: Optional[str]) -> None:
        secret = os.environ.get("INTERNAL_POST_SECRET")
        if not secret:
            raise HTTPException(
                status_code=503,
                detail="internal posting disabled: INTERNAL_POST_SECRET not set",
            )
        if x_internal_auth != secret:
            raise HTTPException(status_code=403, detail="Forbidden")

    @internal_app.post("/internal/hitl/notify")
    async def internal_hitl_notify(
        req: InternalHitlNotifyPayload = Body(),
        x_internal_auth: Optional[str] = Header(None, alias="X-Internal-Auth"),
    ):
        _check_internal_auth(x_internal_auth)

        row = await database.get_pending_hitl(req.pending_id)
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"Pending HITL request #{req.pending_id} not found",
            )
        if row["status"] != "pending":
            return {"success": True, "status": row["status"], "skipped": True}

        from hitl.hitl_discord import send_hitl_approval_dms

        sent_ids = await send_hitl_approval_dms(req.pending_id, bot)
        return {"success": True, "message_ids": sent_ids}

    @internal_app.post("/internal/post")
    async def internal_post(
        # FastAPI 0.139: explicit Body() required alongside Header() on these routes.
        req: InternalPostPayload = Body(),
        x_internal_auth: Optional[str] = Header(None, alias="X-Internal-Auth"),
    ):
        _check_internal_auth(x_internal_auth)

        try:
            channel = await resolve_discord_channel(bot, req.channel_id)

            from discord import DMChannel, Embed, MessageReference
            from discord_chunk import send_chunked

            send_kwargs: dict[str, Any] = {}
            if req.embed:
                send_kwargs["embed"] = Embed.from_dict(req.embed)
            if req.reference_message_id:
                send_kwargs["reference"] = MessageReference(
                    message_id=req.reference_message_id,
                    channel_id=req.channel_id,
                )

            # family-bot-6p6: always chunk when content present (embed on first chunk).
            if req.content:
                message = await send_chunked(
                    channel,
                    req.content,
                    is_dm=isinstance(channel, DMChannel),
                    **send_kwargs,
                )
            else:
                message = await channel.send(**(send_kwargs or {"content": " "}))

            if req.reactions:
                for emoji in req.reactions:
                    await message.add_reaction(emoji)

            return {"success": True, "message_id": message.id}
        except Exception as e:
            logger.error("internal_post failed: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @internal_app.post("/internal/react")
    async def internal_react(
        req: InternalReactPayload = Body(),
        x_internal_auth: Optional[str] = Header(None, alias="X-Internal-Auth"),
    ):
        _check_internal_auth(x_internal_auth)
        try:
            channel = await resolve_discord_channel(bot, req.channel_id)
            message = await channel.fetch_message(req.message_id)
            await message.add_reaction(req.emoji)
            return {"success": True}
        except Exception as e:
            logger.error("internal_react failed: %s", e)
            raise HTTPException(status_code=500, detail=str(e)) from e

    return internal_app