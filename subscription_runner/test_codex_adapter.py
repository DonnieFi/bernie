from __future__ import annotations

import unittest
import queue
import time
from collections import deque
from unittest.mock import MagicMock, patch

from subscription_runner.codex_adapter import (
    AppServerTurn,
    _CONTINUATIONS,
    _dynamic_tools,
    _input_text,
    codex_adapter,
)


class CodexAdapterTest(unittest.TestCase):
    def tearDown(self):
        _CONTINUATIONS.clear()

    def test_normalizes_bernie_tool_schema_for_app_server(self):
        tools = _dynamic_tools([{
            "name": "weather",
            "description": "Current weather",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
            },
        }])
        self.assertEqual(tools[0]["type"], "function")
        self.assertEqual(tools[0]["name"], "weather")
        self.assertIn("city", tools[0]["inputSchema"]["properties"])

    def test_input_contains_history_and_prompt(self):
        text = _input_text({
            "messages": [{"role": "user", "content": "Earlier"}],
            "prompt": "Now",
        })
        self.assertIn("user: Earlier", text)
        self.assertTrue(text.endswith("Now"))

    def test_turn_uses_low_reasoning_effort(self):
        turn = AppServerTurn.__new__(AppServerTurn)
        turn.model = "gpt-5.4"
        turn.payload = {"prompt": "hi"}
        turn.work = MagicMock()
        turn.work.name = "/tmp/work"
        turn.thread_id = ""
        turn._send = MagicMock()
        turn._next_outcome = MagicMock(return_value={"text": "ok"})
        turn._request = MagicMock(side_effect=[
            {},
            {"requiresOpenaiAuth": True, "account": {"type": "chatgpt"}},
            {"thread": {"id": "thread-1"}},
            {},
        ])

        turn.start()

        turn_start = turn._request.call_args_list[3]
        self.assertEqual(turn_start.args[0], "turn/start")
        self.assertEqual(turn_start.args[1]["effort"], "low")

    def test_read_consumes_already_buffered_jsonl(self):
        turn = AppServerTurn.__new__(AppServerTurn)
        turn.cancel_event = None
        turn.deadline = time.monotonic() + 1
        turn.proc = MagicMock()
        turn.proc.poll.return_value = None
        turn.stderr = deque()
        turn.stdout = queue.Queue()
        turn.stdout.put('{"method":"turn/completed"}\n')

        self.assertEqual(turn._read()["method"], "turn/completed")

    @patch("subscription_runner.codex_adapter.AppServerTurn")
    def test_text_completion_closes_turn(self, turn_cls):
        turn = turn_cls.return_value
        turn.start.return_value = {
            "text": "hello",
            "tool_calls": [],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }
        result = codex_adapter({"model": "gpt-5.4", "prompt": "hi", "timeout_s": 30})
        self.assertEqual(result["text"], "hello")
        turn.close.assert_called_once()

    @patch("subscription_runner.codex_adapter.AppServerTurn")
    def test_native_tool_call_is_retained_then_resumed(self, turn_cls):
        turn = turn_cls.return_value
        turn.continuation_id = "resume-secret"
        turn.deadline = time.monotonic() + 30
        turn.start.return_value = {
            "text": None,
            "tool_calls": [{"id": "call-1", "name": "weather", "arguments": {"city": "Halifax"}}],
            "usage": {"input_tokens": 4, "output_tokens": 2},
            "continuation_id": "resume-secret",
        }
        first = codex_adapter({"model": "gpt-5.4", "prompt": "weather", "tools": [{"name": "weather"}]})
        self.assertEqual(first["continuation_id"], "resume-secret")
        turn.close.assert_not_called()
        self.assertIs(_CONTINUATIONS["resume-secret"], turn)

        turn.resume.return_value = {
            "text": "It is sunny.",
            "tool_calls": [],
            "usage": {"input_tokens": 8, "output_tokens": 3},
        }
        second = codex_adapter({
            "model": "gpt-5.4",
            "continuation_id": "resume-secret",
            "tool_results": [{"id": "call-1", "text": "sunny", "success": True}],
        })
        self.assertEqual(second["text"], "It is sunny.")
        turn.resume.assert_called_once()
        turn.close.assert_called_once()
        self.assertNotIn("resume-secret", _CONTINUATIONS)

    def test_unknown_continuation_fails_closed(self):
        result = codex_adapter({
            "model": "gpt-5.4",
            "continuation_id": "missing",
            "tool_results": [{"id": "call-1", "text": "x"}],
        })
        self.assertEqual(result["error"]["code"], "invalid-request")
        self.assertFalse(result["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()
