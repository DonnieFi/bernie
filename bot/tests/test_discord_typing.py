"""Tests for Discord typing heartbeat."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from discord_typing import typing_ack, typing_heartbeat


class TestTypingAck(unittest.IsolatedAsyncioTestCase):
    async def test_fires_trigger_typing(self):
        channel = MagicMock()
        channel.trigger_typing = AsyncMock()
        await typing_ack(channel)
        channel.trigger_typing.assert_awaited_once()

    async def test_swallows_errors(self):
        channel = MagicMock()
        channel.trigger_typing = AsyncMock(side_effect=RuntimeError("nope"))
        await typing_ack(channel)


class TestTypingHeartbeat(unittest.IsolatedAsyncioTestCase):
    async def test_uses_channel_typing_when_available(self):
        channel = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=channel)
        cm.__aexit__ = AsyncMock(return_value=None)
        channel.typing.return_value = cm

        async with typing_heartbeat(channel):
            await asyncio.sleep(0)
        cm.__aenter__.assert_awaited_once()
        cm.__aexit__.assert_awaited_once()
        channel.trigger_typing.assert_not_called()

    async def test_fallback_triggers_and_cleans_up(self):
        channel = MagicMock(spec=["trigger_typing"])
        channel.trigger_typing = AsyncMock()
        async with typing_heartbeat(channel, interval_s=0.05):
            await asyncio.sleep(0.02)
        self.assertGreaterEqual(channel.trigger_typing.await_count, 1)

    async def test_cleanup_on_exception(self):
        channel = MagicMock(spec=["trigger_typing"])
        channel.trigger_typing = AsyncMock()
        with self.assertRaises(RuntimeError):
            async with typing_heartbeat(channel, interval_s=0.05):
                raise RuntimeError("boom")
        self.assertGreaterEqual(channel.trigger_typing.await_count, 1)


if __name__ == "__main__":
    unittest.main()
