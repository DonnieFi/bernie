"""Auth gate for bernie-discord internal post/react routes."""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

sys.modules.setdefault("discord.ext", MagicMock())
sys.modules.setdefault("discord.ext.tasks", MagicMock())


def setUpModule():
    """Install minimal discord types before internal_discord is imported."""
    discord_mod = sys.modules.setdefault("discord", MagicMock())

    class _DMChannel:
        pass

    class _Embed:
        @classmethod
        def from_dict(cls, _data):
            return MagicMock()

    class _MessageReference:
        def __init__(self, **_kwargs):
            pass

    discord_mod.DMChannel = _DMChannel
    discord_mod.Embed = _Embed
    discord_mod.MessageReference = _MessageReference
    assert isinstance(discord_mod.DMChannel, type)


def _asgi_client(app):
    from httpx import ASGITransport, AsyncClient

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestInternalDiscordAuth(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from internal_discord import create_internal_discord_app

        self.app = create_internal_discord_app(MagicMock())

    async def _post(self, path: str, headers=None, json_body=None):
        async with _asgi_client(self.app) as client:
            kwargs = {"headers": headers or {}}
            if json_body is not None:
                kwargs["json"] = json_body
            return await client.post(path, **kwargs)

    async def test_post_missing_secret_returns_503(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("INTERNAL_POST_SECRET", None)
            resp = await self._post(
                "/internal/post",
                headers={"X-Internal-Auth": "anything"},
                json_body={"channel_id": 1, "content": "hi"},
            )
        self.assertEqual(resp.status_code, 503)

    async def test_post_wrong_secret_returns_403(self):
        with patch.dict(os.environ, {"INTERNAL_POST_SECRET": "good-secret"}, clear=False):
            resp = await self._post(
                "/internal/post",
                headers={"X-Internal-Auth": "bad-secret"},
                json_body={"channel_id": 1, "content": "hi"},
            )
        self.assertEqual(resp.status_code, 403)

    async def _react_post(self, headers=None):
        return await self._post(
            "/internal/react",
            headers=headers,
            json_body={"channel_id": 1, "message_id": 2, "emoji": "✅"},
        )

    async def test_react_missing_header_returns_403(self):
        with patch.dict(os.environ, {"INTERNAL_POST_SECRET": "good-secret"}, clear=False):
            resp = await self._react_post(headers={})
        self.assertEqual(resp.status_code, 403)

    async def test_react_missing_secret_returns_503(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("INTERNAL_POST_SECRET", None)
            resp = await self._react_post(headers={"X-Internal-Auth": "x"})
        self.assertEqual(resp.status_code, 503)

    async def test_post_success_returns_message_id(self):
        mock_message = MagicMock()
        mock_message.id = 4242
        mock_channel = MagicMock()
        # internal_post imports send_chunked inside the handler (discord_chunk.send_chunked).
        with patch.dict(os.environ, {"INTERNAL_POST_SECRET": "good-secret"}, clear=False), \
             patch(
                 "internal_discord.resolve_discord_channel",
                 new_callable=AsyncMock,
                 return_value=mock_channel,
             ), \
             patch(
                 "discord_chunk.send_chunked",
                 new_callable=AsyncMock,
                 return_value=mock_message,
             ):
            resp = await self._post(
                "/internal/post",
                headers={"X-Internal-Auth": "good-secret"},
                json_body={"channel_id": 1, "content": "hi"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["message_id"], 4242)


if __name__ == "__main__":
    unittest.main()