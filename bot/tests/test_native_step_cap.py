"""Tests for the NativeToolExecutor step cap (A) and graceful synthesis (B).

A — the loop honours config `executor.max_steps` instead of a hardcoded 5.
B — on exhaustion it makes one tool-less synthesis turn and returns that text,
    instead of dead-ending with "I ran out of steps ... try again."

Mock approach mirrors TestPerIterationLogging in test_caching_integration.py.
"""
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

_BASE_MOCK_MODULES = (
    'discord', 'discord.ext', 'pytz', 'anthropic', 'aiohttp',
    'langfuse_logger', 'config', 'database', 'memory_service', 'tool_gateway',
    'tools', 'person_registry', 'weather_service', 'network_service',
    'calendar_service', 'zoneinfo', 'identity_service', 'food_service',
)
_GOOGLE_MOCK_MODULES = (
    'google', 'google.oauth2', 'google.oauth2.credentials',
    'google_auth_oauthlib', 'google_auth_oauthlib.flow',
    'googleapiclient', 'googleapiclient.discovery',
)


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _ToolBlock:
    type = "tool_use"
    id = "tool_123"
    name = "get_system_health"
    input = {}


class _TextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


def _tool_resp():
    r = MagicMock()
    r.stop_reason = "tool_use"
    r.usage = _FakeUsage()
    r.content = [_ToolBlock()]
    return r


def _text_resp(text):
    r = MagicMock()
    r.stop_reason = "end_turn"
    r.usage = _FakeUsage()
    r.content = [_TextBlock(text)]
    return r


def _empty_end_turn():
    """end_turn with no visible text — e.g. a reasoning model (or-deepseek-v4)
    whose entire output went to a stripped reasoning channel. This is what made
    Bernie reply the canned 'Done!'."""
    r = MagicMock()
    r.stop_reason = "end_turn"
    r.usage = _FakeUsage()
    r.content = []
    return r


class TestNativeStepCap(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mocks = {name: MagicMock() for name in _BASE_MOCK_MODULES + _GOOGLE_MOCK_MODULES}
        self.patcher = patch.dict('sys.modules', self.mocks)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    async def _run(self, *, max_steps, create_side_effect):
        mock_cs = MagicMock()

        async def fake_lf_log(**kwargs):
            return None
        mock_cs._lf_log_generation = fake_lf_log
        mock_cs._call_ollama = AsyncMock()

        with patch.dict('sys.modules', {'claude_service': mock_cs}):
            from executors.native import NativeToolExecutor
            from executor import ExecutorConfig

            gateway = MagicMock()
            gateway.execute = AsyncMock(return_value="tool result")

            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(side_effect=create_side_effect)
            mock_cs._make_client = MagicMock(return_value=mock_client)

            # app_config: model is NOT in ollama_models, so the claude path runs.
            self.mocks['config'].config = {"executor": {"max_steps": max_steps}, "timezone": "UTC"}

            executor = NativeToolExecutor(gateway)
            cfg = ExecutorConfig(surface="chat", model="claude-sonnet-4-6", shadow=False)
            result = await executor.run(
                messages=[{"role": "user", "content": "is ollama on deba up?"}],
                system="You are Bernie.",
                tools=[{"name": "get_system_health"}],
                config=cfg,
            )
            return result, mock_client

    async def test_cap_exhaustion_triggers_synthesis(self):
        """max_steps=2 tool_use turns → 3rd (tool-less) create returns the answer."""
        side_effect = [
            _tool_resp(),                     # loop iter 1
            _tool_resp(),                     # loop iter 2 (cap reached)
            _text_resp("Ollama on Deba is unreachable; I checked health + logs."),  # synthesis
        ]
        result, client = await self._run(max_steps=2, create_side_effect=side_effect)

        self.assertEqual(result, "Ollama on Deba is unreachable; I checked health + logs.")
        self.assertEqual(client.messages.create.await_count, 3)  # 2 loop + 1 synthesis
        # Synthesis call must be tool-less (forces an answer, not more tool calls).
        synth_kwargs = client.messages.create.await_args_list[2].kwargs
        self.assertNotIn("tools", synth_kwargs)

        # Regression guard: the loop ends on a user/tool_result turn, so the
        # synthesis note must ride on THAT message — appending a second `user`
        # turn would be two consecutive same-role messages (Anthropic 400).
        synth_msgs = synth_kwargs["messages"]
        roles = [m["role"] for m in synth_msgs]
        for a, b in zip(roles, roles[1:]):
            self.assertNotEqual(a, b, f"consecutive same-role messages: {roles}")
        self.assertEqual(synth_msgs[-1]["role"], "user")
        note_text = "".join(
            blk.get("text", "")
            for blk in synth_msgs[-1]["content"]
            if isinstance(blk, dict)
        )
        self.assertIn("tool-step limit", note_text)

    async def test_configurable_cap_is_respected(self):
        """With max_steps=3, three tool_use turns happen before synthesis."""
        side_effect = [_tool_resp(), _tool_resp(), _tool_resp(), _text_resp("done")]
        result, client = await self._run(max_steps=3, create_side_effect=side_effect)
        self.assertEqual(result, "done")
        self.assertEqual(client.messages.create.await_count, 4)  # 3 loop + 1 synthesis

    async def test_synthesis_failure_falls_back_to_message(self):
        """If the synthesis turn itself errors, the old message is the last resort."""
        side_effect = [_tool_resp(), _tool_resp(), RuntimeError("api down")]
        result, client = await self._run(max_steps=2, create_side_effect=side_effect)
        self.assertEqual(result, "I ran out of steps trying to complete that. Please try again.")

    async def test_normal_end_turn_within_budget(self):
        """A normal end_turn before the cap returns directly, no synthesis turn."""
        side_effect = [_tool_resp(), _text_resp("Here you go.")]
        result, client = await self._run(max_steps=8, create_side_effect=side_effect)
        self.assertEqual(result, "Here you go.")
        self.assertEqual(client.messages.create.await_count, 2)

    async def test_empty_end_turn_triggers_synthesis_not_done(self):
        """end_turn with no visible text must NOT return 'Done!'. Instead a
        tool-less synthesis turn is forced to produce a real answer."""
        side_effect = [
            _tool_resp(),                                         # gather data
            _empty_end_turn(),                                    # finishes, no text
            _text_resp("Here's your Oura vs Garmin comparison."),  # forced synthesis
        ]
        result, client = await self._run(max_steps=8, create_side_effect=side_effect)
        self.assertEqual(result, "Here's your Oura vs Garmin comparison.")
        self.assertNotEqual(result, "Done!")
        self.assertEqual(client.messages.create.await_count, 3)
        # The synthesis call must be tool-less (forces an answer, not more tools).
        synth_kwargs = client.messages.create.await_args_list[2].kwargs
        self.assertNotIn("tools", synth_kwargs)

    async def test_empty_end_turn_first_turn_synthesizes(self):
        """Empty end_turn on the very first turn (no tools called) still
        recovers via synthesis rather than emitting 'Done!'."""
        side_effect = [_empty_end_turn(), _text_resp("Hi — how can I help?")]
        result, client = await self._run(max_steps=8, create_side_effect=side_effect)
        self.assertEqual(result, "Hi — how can I help?")
        self.assertEqual(client.messages.create.await_count, 2)

    async def test_empty_end_turn_and_empty_synthesis_is_graceful(self):
        """If synthesis also yields nothing, fall back to a graceful message —
        never the canned 'Done!'."""
        side_effect = [_tool_resp(), _empty_end_turn(), _empty_end_turn()]
        result, client = await self._run(max_steps=8, create_side_effect=side_effect)
        self.assertNotEqual(result, "Done!")
        self.assertNotIn("Done!", result)
        self.assertTrue(result.strip())


if __name__ == "__main__":
    unittest.main()
