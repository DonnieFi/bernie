"""family-bot-05u: resolve_discord_channel NotFound → DM; Forbidden not masked."""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Host unittest may lack pydantic; only resolve_discord_channel is under test.
sys.modules.setdefault("pydantic", MagicMock())
sys.modules.setdefault("database", MagicMock())


class _NotFound(Exception):
    status = 404


class _Forbidden(Exception):
    status = 403


_NotFound.__name__ = "NotFound"
_Forbidden.__name__ = "Forbidden"


class TestResolveDiscordChannel(unittest.IsolatedAsyncioTestCase):
    async def test_not_found_falls_back_to_dm(self):
        from internal_discord import resolve_discord_channel

        bot = MagicMock()
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(side_effect=_NotFound("missing channel"))
        user = MagicMock()
        dm = MagicMock()
        user.dm_channel = dm
        bot.get_user.return_value = user

        ch = await resolve_discord_channel(bot, 12345)
        self.assertIs(ch, dm)

    async def test_forbidden_does_not_become_dm(self):
        from internal_discord import resolve_discord_channel

        bot = MagicMock()
        bot.get_channel.return_value = None
        bot.fetch_channel = AsyncMock(side_effect=_Forbidden("no access"))
        bot.get_user = MagicMock()

        with self.assertRaises(_Forbidden):
            await resolve_discord_channel(bot, 99)
        bot.get_user.assert_not_called()
