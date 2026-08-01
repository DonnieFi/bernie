import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import hashlib


# Modules that claude_service / native.py import at top level. Tests mock these
# wholesale so the real implementations don't drag in network / DB clients.
_BASE_MOCK_MODULES = (
    'discord', 'discord.ext', 'pytz', 'anthropic', 'aiohttp',
    'langfuse_logger', 'config', 'database', 'memory_service', 'tool_gateway',
    'tools', 'person_registry', 'weather_service', 'network_service',
    'calendar_service', 'zoneinfo', 'identity_service', 'food_service',
)

# Extra modules required only by tests that import native.py / executors
# (which transitively pulls google-auth via email_service).
_GOOGLE_MOCK_MODULES = (
    'google', 'google.oauth2', 'google.oauth2.credentials',
    'google_auth_oauthlib', 'google_auth_oauthlib.flow',
    'googleapiclient', 'googleapiclient.discovery',
)


def _build_mocks(*extra_module_groups: tuple[str, ...]) -> dict[str, MagicMock]:
    """Return {module_name: MagicMock()} for the base set plus any extras."""
    names = list(_BASE_MOCK_MODULES)
    for group in extra_module_groups:
        names.extend(group)
    return {name: MagicMock() for name in names}


class _MockedModulesMixin:
    """Mixin: install/uninstall the sys.modules patch around each test case.

    Subclasses override ``_mock_module_groups`` to pull in extra mock sets
    (e.g. ``_GOOGLE_MOCK_MODULES``) without duplicating the base list.

    NOTE: tests that exercise behaviour caching (context._read_behaviour_files)
    must call ``context.invalidate_behaviour_cache()`` themselves — the
    module-level cache is intentionally process-global.
    """

    _mock_module_groups: tuple[tuple[str, ...], ...] = ()

    def setUp(self):  # noqa: N802 — unittest convention
        super().setUp()
        self.mocks = _build_mocks(*self._mock_module_groups)
        self.patcher = patch.dict('sys.modules', self.mocks)
        self.patcher.start()

    def tearDown(self):  # noqa: N802
        self.patcher.stop()
        super().tearDown()


class TestCachingIntegration(_MockedModulesMixin, unittest.IsolatedAsyncioTestCase):

    async def test_run_loop_handles_list_system(self):
        """Integration test to ensure _run_loop doesn't crash on list-based system prompts."""
        # Now we can import the real claude_service (its dependencies are mocked)
        import claude_service
        from executor import ExecutorConfig
        
        # Mock the executor to avoid real logic
        mock_executor = MagicMock()
        mock_executor.run = AsyncMock(return_value="Success")
        
        with patch('llm.pipeline.get_executor', return_value=mock_executor):
            system_list = [{"type": "text", "text": "Static", "cache_control": {"type": "ephemeral"}}]
            messages = [{"role": "user", "content": "Hello"}]
            
            # This should NOT raise TypeError
            result = await claude_service._run_loop(
                client=None,
                model="claude-3-5-sonnet",
                system=system_list,
                messages=messages,
                config={"timezone": "America/Halifax"},
                cal_service=None,
                db_module=None,
                tz=None,
                session=None,
                tools=[],
                user_message="Hello"
            )
            
            self.assertEqual(result, "Success")
            
            # Verify prompt_hash was calculated (no TypeError happened)
            args, kwargs = mock_executor.run.call_args
            exec_config = args[3]
            self.assertIsNotNone(exec_config.prompt_hash)
            self.assertEqual(len(exec_config.prompt_hash), 16)

    async def test_call_ollama_flattens_list_system(self):
        """Integration test to ensure _call_ollama flattens list system prompts."""
        import claude_service
        import aiohttp
        
        system_list = [
            {"type": "text", "text": "Part 1", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "Part 2"}
        ]
        messages = [{"role": "user", "content": "Hello"}]
        config = {
            "ollama_base_url": "http://localhost:11434",
            "llm_fallback": {"model": "llama3", "url": "http://localhost:11434"},
            "timezone": "America/Halifax"
        }
        
        # Mock session.post
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"message": {"content": "Ollama response"}})
        
        mock_session = MagicMock()
        mock_session.post.return_value.__aenter__.return_value = mock_resp
        mock_session.closed = False

        # This should NOT raise TypeError during string concatenation
        with patch('llm.context_builder.build_context', return_value={}):
            result = await claude_service._call_ollama(
                system=system_list,
                messages=messages,
                config=config,
                session=mock_session
            )
            
            self.assertEqual(result, "Ollama response")
            
            # Verify the payload sent to Ollama
            call_args = mock_session.post.call_args
            payload = call_args.kwargs['json']
            system_msg = payload['messages'][0]['content']
            self.assertIn("Part 1", system_msg)
            self.assertIn("Part 2", system_msg)

class TestHistoryPruning(_MockedModulesMixin, unittest.TestCase):
    """_prune_old_tool_results must strip tool_result content from older messages."""

    def _make_tool_result_msg(self, tool_use_id="tu_1", content="big result data"):
        return {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
        }

    def _make_text_msg(self, role="user", text="hello"):
        return {"role": role, "content": text}

    def test_short_history_unchanged(self):
        from claude_service import _prune_old_tool_results, _HISTORY_VERBATIM_TAIL
        msgs = [self._make_tool_result_msg(f"tu_{i}") for i in range(_HISTORY_VERBATIM_TAIL)]
        result = _prune_old_tool_results(msgs)
        self.assertEqual(len(result), len(msgs))
        # Content should be unchanged since all are within the tail
        for r, orig in zip(result, msgs):
            self.assertEqual(r["content"], orig["content"])

    def test_old_tool_results_stubbed(self):
        from claude_service import _prune_old_tool_results, _TOOL_RESULT_STUB, _HISTORY_VERBATIM_TAIL
        # Build more messages than the tail
        msgs = []
        for i in range(_HISTORY_VERBATIM_TAIL + 2):
            msgs.append(self._make_tool_result_msg(f"tu_{i}", content=f"big data {i}"))

        result = _prune_old_tool_results(msgs)
        self.assertEqual(len(result), len(msgs))

        # First 2 messages are outside the tail → content must be stubbed
        for m in result[:2]:
            block = m["content"][0]
            self.assertEqual(block["content"], _TOOL_RESULT_STUB)
            self.assertIn("tool_use_id", block)  # pairing structure preserved

        # Last TAIL messages must have original content
        for i, m in enumerate(result[-_HISTORY_VERBATIM_TAIL:]):
            block = m["content"][0]
            expected_idx = 2 + i
            self.assertEqual(block["content"], f"big data {expected_idx}")

    def test_text_messages_untouched(self):
        from claude_service import _prune_old_tool_results, _HISTORY_VERBATIM_TAIL
        msgs = [self._make_text_msg("user", f"msg {i}") for i in range(_HISTORY_VERBATIM_TAIL + 3)]
        result = _prune_old_tool_results(msgs)
        for r, orig in zip(result, msgs):
            self.assertEqual(r["content"], orig["content"])


class TestToolSchemaSort(unittest.TestCase):
    """get_tool_schemas() must return tools sorted alphabetically by name."""

    def test_schemas_sorted_alphabetically(self):
        import sys
        sys.path.insert(0, '/opt/family-bot/bot')
        from tool_gateway import ToolGateway
        from tools import get_registry, load_all_domains
        load_all_domains()
        gw = ToolGateway(registry=get_registry())
        schemas = gw.get_tool_schemas("family")
        names = [s["name"] for s in schemas]
        self.assertEqual(names, sorted(names), "Tool schemas must be sorted alphabetically")

    def test_domain_filter_applied(self):
        import sys
        sys.path.insert(0, '/opt/family-bot/bot')
        from tool_gateway import ToolGateway
        from tools import get_registry, load_all_domains
        load_all_domains()
        gw = ToolGateway(registry=get_registry())
        schemas = gw.get_tool_schemas("admin", domains=["weather"])
        names = {s["name"] for s in schemas}
        self.assertIn("get_current_weather", names)
        # Non-weather tools must be filtered out
        self.assertNotIn("get_todays_events", names)
        self.assertNotIn("control_device", names)


class TestPerIterationLogging(_MockedModulesMixin, unittest.IsolatedAsyncioTestCase):
    """S2: N tool-use iterations must produce N+1 _lf_log_generation calls."""

    _mock_module_groups = (_GOOGLE_MOCK_MODULES,)

    async def test_two_tool_iterations_produce_three_log_calls(self):
        """2 tool_use iterations + 1 end_turn = 3 _lf_log_generation calls."""
        log_calls = []

        async def fake_lf_log(**kwargs):
            log_calls.append(kwargs)

        with patch("llm.observability.log_llm_turn", side_effect=fake_lf_log):
            from executors.native import NativeToolExecutor
            from executor import ExecutorConfig, ServiceRefs
            from unittest.mock import MagicMock as _MM

            gateway = _MM()
            gateway.execute = AsyncMock(return_value="tool result")

            class _FakeUsage:
                input_tokens = 10
                output_tokens = 5
                cache_creation_input_tokens = 0
                cache_read_input_tokens = 0

            class _ToolBlock:
                type = "tool_use"
                id = "tool_123"
                name = "get_todays_events"
                input = {}

            class _TextBlock:
                type = "text"
                text = "Here is your answer."

            # Response sequence: tool_use → tool_use → end_turn
            resp_tool1 = MagicMock()
            resp_tool1.stop_reason = "tool_use"
            resp_tool1.usage = _FakeUsage()
            resp_tool1.content = [_ToolBlock()]

            resp_tool2 = MagicMock()
            resp_tool2.stop_reason = "tool_use"
            resp_tool2.usage = _FakeUsage()
            resp_tool2.content = [_ToolBlock()]

            resp_end = MagicMock()
            resp_end.stop_reason = "end_turn"
            resp_end.usage = _FakeUsage()
            resp_end.content = [_TextBlock()]

            mock_client = MagicMock()
            executor = NativeToolExecutor(gateway).with_services(
                ServiceRefs(llm_for=lambda _model: mock_client)
            )
            self.mocks['config'].config = {"timezone": "UTC"}

            mock_client.messages.create = AsyncMock(
                side_effect=[resp_tool1, resp_tool2, resp_end]
            )

            cfg = ExecutorConfig(
                surface="chat",
                model="claude-sonnet-4-6",
                shadow=False,
                session_id="test-s",
                conversation_id="test-c",
            )

            result = await executor.run(
                messages=[{"role": "user", "content": "What's on today?"}],
                system="You are Bernie.",
                tools=[],
                config=cfg,
            )
            # asyncio.create_task schedules on next iteration; flush the loop
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            self.assertEqual(result, "Here is your answer.")
            # 2 tool_use iterations + 1 end_turn = 3 log calls
            # (shadow=False so all iterations log)
            self.assertEqual(len(log_calls), 3, f"Expected 3 log calls, got {len(log_calls)}")
            # Verify token sums are present in each call
            for call in log_calls:
                self.assertIn("input_tokens", call)
                self.assertIn("output_tokens", call)

    async def test_shadow_iterations_log_with_shadow_surface(self):
        """shadow=True logs usage separately under the shadow surface."""
        log_calls = []

        async def fake_lf_log(**kwargs):
            log_calls.append(kwargs)

        with patch("llm.observability.log_llm_turn", side_effect=fake_lf_log):
            from executors.native import NativeToolExecutor
            from executor import ExecutorConfig, ServiceRefs

            gateway = MagicMock()

            class _FakeUsage:
                input_tokens = 10
                output_tokens = 5
                cache_creation_input_tokens = 0
                cache_read_input_tokens = 0

            class _TextBlock:
                type = "text"
                text = "Shadow answer."

            resp_end = MagicMock()
            resp_end.stop_reason = "end_turn"
            resp_end.usage = _FakeUsage()
            resp_end.content = [_TextBlock()]

            mock_client = MagicMock()
            executor = NativeToolExecutor(gateway).with_services(
                ServiceRefs(llm_for=lambda _model: mock_client)
            )
            self.mocks['config'].config = {"timezone": "UTC"}
            mock_client.messages.create = AsyncMock(return_value=resp_end)

            cfg = ExecutorConfig(
                surface="chat",
                model="claude-sonnet-4-6",
                shadow=True,
            )

            await executor.run(
                messages=[{"role": "user", "content": "hi"}],
                system="system",
                tools=[],
                config=cfg,
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            self.assertEqual(len(log_calls), 1)
            self.assertEqual(log_calls[0].get("surface"), "shadow")


if __name__ == "__main__":
    unittest.main()
