"""family-bot-hhf: internal Discord RPC retries 502/503 briefly."""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

sys.modules.setdefault("aiohttp", MagicMock())


class TestPostJsonHttpRetry(unittest.IsolatedAsyncioTestCase):
    async def test_retries_503_then_succeeds(self):
        import aiohttp
        from cross_container import _post_json

        # Ensure except clause still matches real connector types when present
        if not hasattr(aiohttp, "ClientConnectorError"):
            class _CCE(OSError):
                pass
            aiohttp.ClientConnectorError = _CCE

        ok_resp = AsyncMock()
        ok_resp.status = 200
        ok_resp.json = AsyncMock(return_value={"message_id": 1})
        ok_resp.__aenter__ = AsyncMock(return_value=ok_resp)
        ok_resp.__aexit__ = AsyncMock(return_value=False)

        bad_resp = AsyncMock()
        bad_resp.status = 503
        bad_resp.text = AsyncMock(return_value="unavailable")
        bad_resp.__aenter__ = AsyncMock(return_value=bad_resp)
        bad_resp.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(side_effect=[bad_resp, ok_resp])

        with patch.dict(os.environ, {"INTERNAL_POST_SECRET": "test-secret"}), \
             patch("cross_container.get_http_session", return_value=session), \
             patch("cross_container.asyncio.sleep", new_callable=AsyncMock):
            data = await _post_json("http://example/internal/post", {"channel_id": 1})
        self.assertEqual(data["message_id"], 1)
        self.assertEqual(session.post.call_count, 2)
