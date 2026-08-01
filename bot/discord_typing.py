"""Discord typing indicator — immediate ack + sustained heartbeat for long turns."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

log = logging.getLogger(__name__)


async def typing_ack(channel) -> None:
    """Fire typing once right away so the user knows the message landed."""
    with contextlib.suppress(Exception):
        await channel.trigger_typing()


@asynccontextmanager
async def typing_heartbeat(channel, interval_s: float = 8.0) -> AsyncIterator[None]:
    """Keep typing active for long LLM turns.

    Prefer discord.py's ``channel.typing()`` loop when available; fall back to
    manual ``trigger_typing`` refresh for test mocks.
    """
    typing_cm = getattr(channel, "typing", None)
    if callable(typing_cm):
        async with typing_cm():
            yield
        return

    stop = asyncio.Event()

    async def _loop() -> None:
        while not stop.is_set():
            try:
                await channel.trigger_typing()
            except Exception as exc:
                log.debug("typing_heartbeat trigger failed: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(_loop())
    try:
        await typing_ack(channel)
        yield
    finally:
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
