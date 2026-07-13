import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

# Specialized mock for claude_service to handle async calls
mock_claude = MagicMock()
mock_claude._lf_log_generation = AsyncMock()
mock_claude._make_client = MagicMock()

class TestCachingFallback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Localize mocks to prevent poisoning other tests
        self.mocks = {
            'discord': MagicMock(),
            'discord.ext': MagicMock(),
            'pytz': MagicMock(),
            'aiosqlite': MagicMock(),
            'anthropic': MagicMock(),
            'aiohttp': MagicMock(),
            'google': MagicMock(),
            'google.oauth2': MagicMock(),
            'google.oauth2.credentials': MagicMock(),
            'google_auth_oauthlib': MagicMock(),
            'google_auth_oauthlib.flow': MagicMock(),
            'googleapiclient': MagicMock(),
            'googleapiclient.discovery': MagicMock(),
            'langfuse_logger': MagicMock(),
            'config': MagicMock(),
            'database': MagicMock(),
            'memory_service': MagicMock(),
            'tool_gateway': MagicMock(),
            'tools': MagicMock(),
            'person_registry': MagicMock(),
            'weather_service': MagicMock(),
            'network_service': MagicMock(),
            'calendar_service': MagicMock(),
            'zoneinfo': MagicMock(),
            'claude_service': mock_claude
        }
        self.patcher = patch.dict('sys.modules', self.mocks)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    async def asyncSetUp(self):
        from executors.native import NativeToolExecutor
        from executor import ExecutorConfig
        
        self.gateway = MagicMock()
        self.executor = NativeToolExecutor(self.gateway)
        self.config = ExecutorConfig(
            surface="chat",
            model="claude-3-5-sonnet-20240620",
            conversation_id="test-conv"
        )
        
        # Provide a real value for timezone
        self.mocks['config'].config = {"timezone": "UTC"}
        self.mock_log = mock_claude._lf_log_generation
        self.mock_log.reset_mock()

    async def asyncTearDown(self):
        pass

    async def test_successful_caching_extraction(self):
        """Test that cache tokens are correctly extracted from a successful response."""
        import claude_service
        mock_client = MagicMock()
        claude_service._make_client.return_value = mock_client
        
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_response.usage.cache_creation_input_tokens = 20
        mock_response.usage.cache_read_input_tokens = 80
        mock_response.stop_reason = "end_turn"
        class MockTextBlock:
            def __init__(self, text):
                self.text = text
                self.type = "text"

        mock_response.content = [MockTextBlock("Cached response")]
        
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        
        system = [{"type": "text", "text": "Static", "cache_control": {"type": "ephemeral"}}]
        
        result = await self.executor.run([], system, [], self.config)
        
        self.assertEqual(result, "Cached response")
        # Verify logging was called with correct cache tokens
        self.mock_log.assert_called_once()
        kwargs = self.mock_log.call_args.kwargs
        self.assertEqual(kwargs['cache_creation_tokens'], 20)
        self.assertEqual(kwargs['cache_read_tokens'], 80)

    async def test_fallback_on_400_error(self):
        """Test that cache_control is stripped and request retried on 400 error."""
        import claude_service
        mock_client = MagicMock()
        claude_service._make_client.return_value = mock_client
        
        # 1. First call fails with 400 (unsupported caching)
        error_400 = Exception("BadRequestError: 400 - cache_control is not supported by this model")
        
        # 2. Second call succeeds
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_response.stop_reason = "end_turn"
        class MockTextBlock:
            def __init__(self, text):
                self.text = text
                self.type = "text"

        mock_response.content = [MockTextBlock("Fallback response")]
        
        mock_client.messages.create = AsyncMock(side_effect=[error_400, mock_response])
        
        system = [{"type": "text", "text": "Static", "cache_control": {"type": "ephemeral"}}]
        
        result = await self.executor.run([], system, [], self.config)
        
        self.assertEqual(result, "Fallback response")
        self.assertEqual(mock_client.messages.create.call_count, 2)
        
        # Verify cache_control was stripped in the second call
        second_call_system = mock_client.messages.create.call_args_list[1].kwargs['system']
        self.assertNotIn("cache_control", second_call_system[0])

    async def test_litellm_compatibility(self):
        """Test that LiteLLM/OpenRouter routes also trigger fallback if they reject markers."""
        import claude_service
        self.config.model = "openrouter/anthropic/claude-3-haiku"
        mock_client = MagicMock()
        claude_service._make_client.return_value = mock_client
        
        # Simulate LiteLLM passing through a 400 from a provider
        error_400 = Exception("LiteLLM Error: 400 - Provider does not support prompt caching")
        
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 50
        mock_response.usage.output_tokens = 10
        mock_response.stop_reason = "end_turn"
        class MockTextBlock:
            def __init__(self, text):
                self.text = text
                self.type = "text"

        mock_response.content = [MockTextBlock("LiteLLM Fallback")]
        
        mock_client.messages.create = AsyncMock(side_effect=[error_400, mock_response])
        
        system = [{"type": "text", "text": "Static", "cache_control": {"type": "ephemeral"}}]
        
        result = await self.executor.run([], system, [], self.config)
        
        self.assertEqual(result, "LiteLLM Fallback")
        self.assertEqual(mock_client.messages.create.call_count, 2)

if __name__ == "__main__":
    unittest.main()
