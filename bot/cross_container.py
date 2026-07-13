"""Wave 2b cross-container helpers (cognition → discord posting).

This module provides the client side for the internal /internal/post endpoint
that runs inside the bernie-discord container on port 9000 (only reachable via
the bernie-net Docker network).
"""

import asyncio
import os
from typing import Any

import aiohttp

from http_session import INTERNAL_POST_TIMEOUT, get_http_session

_RPC_MAX_ATTEMPTS = 8
_RPC_BACKOFF_S = 2
# Short-cap HTTP retries during discord restart (family-bot-hhf); separate from connector backoff.
_HTTP_RETRY_STATUSES = frozenset({502, 503})
_HTTP_RETRY_MAX = 3
_HTTP_RETRY_BACKOFF_S = 1.0


class PostedMessage:
    """ponytail: discord.Message stand-in for /internal/post; upgrade = real Message passthrough."""

    __slots__ = ("id", "_channel_id")

    def __init__(self, message_id: int, channel_id: int):
        self.id = message_id
        self._channel_id = channel_id

    async def add_reaction(self, emoji: str) -> None:
        await add_message_reaction(self._channel_id, self.id, emoji)


def _internal_discord_base() -> str:
    env_url = os.environ.get("INTERNAL_DISCORD_URL")
    if env_url:
        return env_url.rstrip("/")
    try:
        from config import load_config

        cfg_url = load_config().get("internal_discord_url")
        if cfg_url:
            return str(cfg_url).rstrip("/")
    except Exception:
        pass
    return "http://bernie-discord:9000"


def internal_discord_post_url() -> str:
    return f"{_internal_discord_base()}/internal/post"


def _internal_headers() -> dict[str, str]:
    secret = os.environ.get("INTERNAL_POST_SECRET")
    return {"X-Internal-Auth": secret} if secret else {}


async def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to bernie-discord internal route with connection + short 502/503 retry."""
    if not os.environ.get("INTERNAL_POST_SECRET"):
        raise RuntimeError("INTERNAL_POST_SECRET not set — cross-container Discord disabled")
    session = get_http_session()
    last_err: Exception | None = None
    http_tries = 0
    for attempt in range(1, _RPC_MAX_ATTEMPTS + 1):
        try:
            # family-bot-1bf.1: per-request timeout even if session defaults are missing
            async with session.post(
                url,
                json=payload,
                headers=_internal_headers(),
                timeout=INTERNAL_POST_TIMEOUT,
            ) as resp:
                if resp.status in _HTTP_RETRY_STATUSES and http_tries < _HTTP_RETRY_MAX - 1:
                    http_tries += 1
                    text = await resp.text()
                    last_err = RuntimeError(
                        f"internal request failed ({resp.status}): {text}"
                    )
                    await asyncio.sleep(_HTTP_RETRY_BACKOFF_S * http_tries)
                    continue
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"internal request failed ({resp.status}): {text}")
                return await resp.json()
        except (aiohttp.ClientConnectorError, OSError) as exc:
            last_err = exc
            if attempt >= _RPC_MAX_ATTEMPTS:
                break
            await asyncio.sleep(_RPC_BACKOFF_S * attempt)
    raise RuntimeError(f"internal request unreachable after {_RPC_MAX_ATTEMPTS} attempts: {last_err}")


async def add_message_reaction(channel_id: int, message_id: int, emoji: str) -> None:
    """Add a reaction via bernie-discord /internal/react."""
    url = f"{_internal_discord_base()}/internal/react"
    await _post_json(
        url,
        {"channel_id": channel_id, "message_id": message_id, "emoji": emoji},
    )


def discord_client_ready(bot) -> bool:
    """True when the discord.Client is logged in (bernie-discord / monolith only)."""
    return hasattr(bot, "is_ready") and bot.is_ready()


async def post_to_anvil(content: str, *, bot, config: dict) -> None:
    """Post a message to #anvil — live client on discord, cross-container elsewhere."""
    anvil_id = config.get("anvil_channel_id")
    if not anvil_id:
        return
    if discord_client_ready(bot):
        channel = bot.get_channel(int(anvil_id))
        if channel is None and hasattr(bot, "fetch_channel"):
            channel = await bot.fetch_channel(int(anvil_id))
        if channel is not None:
            await channel.send(content)
            return
    await post_to_discord(int(anvil_id), content=content)


async def post_to_discord(
    channel_id: int,
    content: str | None = None,
    embed: dict | None = None,
    reference_message_id: int | None = None,
    reactions: list[str] | None = None,
) -> PostedMessage:
    """Post a message to Discord via the internal endpoint in bernie-discord.

    Used by cognition tasks (e.g. nightly_eval, dead_letter_digest) during the
    Wave 2b shadow period so they can produce output without running the full
    Discord client.

    Example:
        # In nightly_eval_task or similar:
        # msg_id = await post_to_discord(anvil_id, content=digest)
    """
    payload: dict[str, Any] = {"channel_id": channel_id}
    if content:
        payload["content"] = content
    if embed:
        payload["embed"] = embed
    if reference_message_id:
        payload["reference_message_id"] = reference_message_id
    if reactions:
        payload["reactions"] = reactions

    data = await _post_json(internal_discord_post_url(), payload)
    return PostedMessage(data["message_id"], channel_id)


async def notify_hitl_pending(pending_id: int) -> None:
    """Send a request to the internal Discord endpoint to notify about a pending HITL hold."""
    if os.environ.get("BERNIE_DISABLE_HITL_DM") == "1":
        return

    payload = {"pending_id": pending_id}
    url = f"{_internal_discord_base()}/internal/hitl/notify"

    await _post_json(url, payload)
