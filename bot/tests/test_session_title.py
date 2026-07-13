"""Tests for session title generation — verifies direct API call, not chat_general."""
import asyncio
import sys
import os
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

_HERE = os.path.dirname(__file__)
_BOT_DIR = os.path.abspath(os.path.join(_HERE, ".."))
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

_MOCK_MODULES = [
    "discord", "discord.ext", "discord.ext.commands", "discord.ext.tasks",
    "anthropic", "aiohttp",
    "googleapiclient", "googleapiclient.discovery",
    "google.oauth2", "google.auth.transport.requests",
    "google.oauth2.credentials", "google_auth_oauthlib.flow",
    "croniter", "websockets", "pytz",
]
for _mod in _MOCK_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def _db_patches(*, snippet=None, cached=None):
    """Patch DB read + routed write paths used by activity_aggregator."""
    mock_db = MagicMock()
    mock_db.get_cached_session_title = AsyncMock(return_value=cached)
    mock_db.conversation_snippet_for_title = AsyncMock(return_value=snippet or [])
    mock_routed = AsyncMock()
    return (
        patch("activity_aggregator.get_database", return_value=mock_db),
        patch("activity_aggregator.db_writes.routed", mock_routed),
        mock_db,
        mock_routed,
    )


def _cache_title(mock_routed):
    """Return title arg from the cache_session_title routed write, if any."""
    for call in mock_routed.call_args_list:
        if call.args and call.args[0] == "cache_session_title":
            return call.args[2]
    return None


class TestSessionTitleGeneration(unittest.IsolatedAsyncioTestCase):

    async def test_title_uses_direct_api_not_chat_general(self):
        """generate_and_cache_session_title must NOT call chat_general."""
        from activity_aggregator import generate_and_cache_session_title

        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text="Weather Planning Chat")]
        fake_resp.usage = MagicMock(
            input_tokens=50, output_tokens=5,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )

        fake_client = MagicMock()
        fake_client.messages = MagicMock()
        fake_client.messages.create = AsyncMock(return_value=fake_resp)

        snippet = [
            {"role": "user", "content": "what's the weather like today?"},
            {"role": "assistant", "content": "It's sunny and 18°C."},
        ]
        patches = _db_patches(snippet=snippet, cached=None)
        with patches[0], patches[1] as mock_routed, \
             patch("llm.clients.make_client", return_value=fake_client), \
             patch("llm.clients.close_client", new_callable=AsyncMock), \
             patch("llm.runtime.get_container", return_value=None), \
             patch("llm.chat.chat_general", new_callable=AsyncMock) as mock_chat_general:

            await generate_and_cache_session_title(
                session_id="222222222222222222-1748194200000",
                channel_id=222222222222222222,
                start_time=1748194200.0,
            )

        mock_chat_general.assert_not_called()
        self.assertEqual(_cache_title(mock_routed), "Weather Planning Chat")

    async def test_title_strips_quotes_and_whitespace(self):
        """Title returned with surrounding quotes/spaces is cleaned before caching."""
        from activity_aggregator import generate_and_cache_session_title

        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text='"  Homework Help Session  "')]
        fake_resp.usage = MagicMock(
            input_tokens=50, output_tokens=5,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )

        fake_client = MagicMock()
        fake_client.messages = MagicMock()
        fake_client.messages.create = AsyncMock(return_value=fake_resp)

        patches = _db_patches(
            snippet=[{"role": "user", "content": "can you help with my math homework?"}],
        )
        with patches[0], patches[1] as mock_routed, \
             patch("llm.clients.make_client", return_value=fake_client), \
             patch("llm.clients.close_client", new_callable=AsyncMock), \
             patch("llm.runtime.get_container", return_value=None):

            await generate_and_cache_session_title(
                session_id="222222222222222222-1748194200000",
                channel_id=222222222222222222,
                start_time=1748194200.0,
            )

        self.assertEqual(_cache_title(mock_routed), "Homework Help Session")

    async def test_no_history_rows_skips_api_call(self):
        """If conversation_history has no rows, no API call is made."""
        from activity_aggregator import generate_and_cache_session_title

        fake_client = MagicMock()
        fake_client.messages = MagicMock()
        fake_client.messages.create = AsyncMock()

        patches = _db_patches(snippet=[])
        with patches[0], patches[1] as mock_routed, \
             patch("llm.clients.make_client", return_value=fake_client):

            await generate_and_cache_session_title(
                session_id="222222222222222222-1748194200000",
                channel_id=222222222222222222,
                start_time=1748194200.0,
            )

        fake_client.messages.create.assert_not_called()
        self.assertIsNone(_cache_title(mock_routed))

    async def test_shadow_calls_fallback_used_when_history_pruned(self):
        """Falls back to shadow_calls when conversation_history has no rows."""
        from activity_aggregator import generate_and_cache_session_title

        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text="Presence Location Check")]
        fake_resp.usage = MagicMock(
            input_tokens=40, output_tokens=4,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )

        fake_client = MagicMock()
        fake_client.messages = MagicMock()
        fake_client.messages.create = AsyncMock(return_value=fake_resp)

        # conversation_snippet_for_title implements shadow fallback internally
        snippet = [{"role": "user", "content": "Where is Mom?"}]
        patches = _db_patches(snippet=snippet)
        with patches[0], patches[1] as mock_routed, \
             patch("llm.clients.make_client", return_value=fake_client), \
             patch("llm.clients.close_client", new_callable=AsyncMock), \
             patch("llm.runtime.get_container", return_value=None):

            await generate_and_cache_session_title(
                session_id="111111111111111111-1779475199832",
                channel_id=111111111111111111,
                start_time=1779475199.0,
            )

        self.assertEqual(_cache_title(mock_routed), "Presence Location Check")

    async def test_already_cached_skips_api_call(self):
        """If a title is already cached, no API call is made."""
        from activity_aggregator import generate_and_cache_session_title

        fake_client = MagicMock()
        fake_client.messages = MagicMock()
        fake_client.messages.create = AsyncMock()

        mock_db = MagicMock()
        mock_db.get_cached_session_title = AsyncMock(return_value="Existing Title")
        with patch("activity_aggregator.get_database", return_value=mock_db), \
             patch("llm.clients.make_client", return_value=fake_client):

            await generate_and_cache_session_title(
                session_id="222222222222222222-1748194200000",
                channel_id=222222222222222222,
                start_time=1748194200.0,
            )

        fake_client.messages.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
