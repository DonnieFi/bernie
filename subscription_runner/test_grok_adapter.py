"""Unit tests for the headless Grok CLI adapter (text path)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from subscription_runner.grok_adapter import (  # noqa: E402
    _parse_json_output,
    _prompt_from_payload,
    build_grok_argv,
    grok_adapter,
    run_grok_cli,
)


class TestGrokAdapter(unittest.TestCase):
    def test_build_argv_disables_tools_and_pins_model(self):
        argv = build_grok_argv("grok-4.5", "/tmp/empty", prompt_file="/tmp/empty/prompt.txt")
        self.assertEqual(argv[0], "grok")
        self.assertIn("--prompt-file", argv)
        self.assertEqual(argv[argv.index("--prompt-file") + 1], "/tmp/empty/prompt.txt")
        self.assertNotIn("hello", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)
        self.assertIn("--no-subagents", argv)
        self.assertIn("--disable-web-search", argv)
        self.assertIn("--disallowed-tools", argv)
        self.assertEqual(argv[argv.index("--max-turns") + 1], "1")
        joined = " ".join(argv)
        self.assertNotIn(";", joined)
        self.assertNotIn("|", joined)

    def test_tool_max_turns_scales_with_prompt_and_catalog(self):
        from subscription_runner.grok_adapter import _tool_max_turns

        self.assertEqual(_tool_max_turns(prompt="x" * 1000, tool_count=0), 1)
        self.assertEqual(_tool_max_turns(prompt="x" * 1000, tool_count=5), 3)
        self.assertEqual(_tool_max_turns(prompt="x" * 25000, tool_count=112), 6)

    def test_prompt_includes_system_and_history_when_prompt_set(self):
        text = _prompt_from_payload({
            "system": "You are Bernie.",
            "prompt": "weather in Halifax?",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
        })
        self.assertIn("You are Bernie.", text)
        self.assertIn("user: hi", text)
        self.assertIn("assistant: hello", text)
        self.assertIn("user: weather in Halifax?", text)

    def test_parse_json_success_and_usage(self):
        raw = json.dumps({
            "text": "OK",
            "stopReason": "EndTurn",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        })
        parsed = _parse_json_output(raw)
        self.assertEqual(parsed["text"], "OK")
        self.assertEqual(parsed["usage"]["input_tokens"], 10)
        self.assertEqual(parsed["usage"]["output_tokens"], 2)

    def test_parse_unknown_usage_not_fabricated(self):
        parsed = _parse_json_output(json.dumps({"text": "hi"}))
        self.assertEqual(parsed["usage"]["input_tokens"], None)
        self.assertEqual(parsed["usage"]["output_tokens"], None)

    def test_parse_malformed_json_object(self):
        parsed = _parse_json_output("[1,2,3]")
        self.assertIn("error", parsed)
        self.assertEqual(parsed["error"]["code"], "malformed")

    def test_success_via_mocked_cli(self):
        stdout = json.dumps({
            "text": "hello from grok",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }).encode()

        proc = MagicMock()
        proc.communicate.return_value = (stdout, b"")
        proc.returncode = 0
        proc.pid = 4242

        with (
            patch("subscription_runner.grok_adapter.shutil.which", return_value="/usr/bin/grok"),
            patch("subscription_runner.grok_adapter.subprocess.Popen", return_value=proc) as popen,
        ):
            result = grok_adapter({
                "provider": "grok",
                "model": "grok-4.5",
                "surface": "chat",
                "prompt": "Say hi",
            })

        self.assertEqual(result["provider"], "grok")
        self.assertEqual(result["text"], "hello from grok")
        self.assertEqual(result["usage"]["input_tokens"], 5)
        self.assertNotIn("error", result)
        args = popen.call_args[0][0]
        self.assertEqual(args[0], "/usr/bin/grok")
        self.assertIn("start_new_session", popen.call_args.kwargs)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_timeout_kills_process_group(self):
        proc = MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="grok", timeout=1)
        proc.pid = 99
        proc.wait.return_value = -9

        with (
            patch("subscription_runner.grok_adapter.shutil.which", return_value="/usr/bin/grok"),
            patch("subscription_runner.grok_adapter.subprocess.Popen", return_value=proc),
            patch("subscription_runner.grok_adapter.os.killpg") as killpg,
        ):
            result = run_grok_cli("grok-4.5", "long", timeout_s=1)

        self.assertEqual(result["error"]["code"], "timeout")
        self.assertTrue(result["error"]["retryable"])
        killpg.assert_called_once_with(99, signal.SIGKILL)

    def test_auth_failure_classified(self):
        proc = MagicMock()
        proc.communicate.return_value = (b"", b"Not authenticated. Run grok login")
        proc.returncode = 1
        proc.pid = 1
        with (
            patch("subscription_runner.grok_adapter.shutil.which", return_value="/usr/bin/grok"),
            patch("subscription_runner.grok_adapter.subprocess.Popen", return_value=proc),
        ):
            result = grok_adapter({
                "provider": "grok", "model": "grok-4.5", "surface": "chat", "prompt": "x",
            })
        self.assertEqual(result["error"]["code"], "auth")
        self.assertTrue(result["error"]["retryable"])

    def test_quota_failure_classified(self):
        proc = MagicMock()
        proc.communicate.return_value = (b"", b"Error: rate limit / quota exceeded 429")
        proc.returncode = 1
        proc.pid = 1
        with (
            patch("subscription_runner.grok_adapter.shutil.which", return_value="/usr/bin/grok"),
            patch("subscription_runner.grok_adapter.subprocess.Popen", return_value=proc),
        ):
            result = run_grok_cli("grok-4.5", "x")
        self.assertEqual(result["error"]["code"], "quota")

    def test_busy_classified(self):
        proc = MagicMock()
        proc.communicate.return_value = (b"", b"resource temporarily busy")
        proc.returncode = 1
        proc.pid = 1
        with (
            patch("subscription_runner.grok_adapter.shutil.which", return_value="/usr/bin/grok"),
            patch("subscription_runner.grok_adapter.subprocess.Popen", return_value=proc),
        ):
            result = run_grok_cli("grok-4.5", "x")
        self.assertEqual(result["error"]["code"], "busy")

    def test_missing_binary_unavailable(self):
        with patch("subscription_runner.grok_adapter.shutil.which", return_value=None):
            result = grok_adapter({
                "provider": "grok", "model": "grok-4.5", "surface": "chat", "prompt": "x",
            })
        self.assertEqual(result["error"]["code"], "unavailable")

    def test_invalid_payload(self):
        result = grok_adapter({"provider": "grok", "model": "grok-4.5", "surface": "chat"})
        self.assertEqual(result["error"]["code"], "invalid-request")

    def test_workdir_is_temp_not_repo(self):
        captured = {}

        def fake_popen(argv, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            proc = MagicMock()
            proc.communicate.return_value = (json.dumps({"text": "ok"}).encode(), b"")
            proc.returncode = 0
            proc.pid = 7
            return proc

        with (
            patch("subscription_runner.grok_adapter.shutil.which", return_value="/usr/bin/grok"),
            patch("subscription_runner.grok_adapter.subprocess.Popen", side_effect=fake_popen),
        ):
            grok_adapter({
                "provider": "grok", "model": "grok-4.5", "surface": "chat", "prompt": "x",
            })
        self.assertTrue(str(captured["cwd"]).startswith(tempfile.gettempdir()))
        self.assertNotIn("family-bot", str(captured["cwd"]))

    def test_parse_markdown_fenced_tool_envelope(self):
        raw = json.dumps({
            "text": '```json\n{"type":"tool_calls","tool_calls":[{"name":"get_weather","arguments":{"when":"today"}}]}\n```',
            "usage": {"input_tokens": 3, "output_tokens": 1},
        })
        parsed = _parse_json_output(raw, expect_envelope=True)
        self.assertEqual(parsed["tool_calls"][0]["name"], "get_weather")

    def test_tool_envelope_parsed(self):
        stdout = json.dumps({
            "text": json.dumps({
                "type": "tool_calls",
                "tool_calls": [{"id": "1", "name": "get_weather", "arguments": {"city": "Halifax"}}],
            }),
            "structuredOutput": {
                "type": "tool_calls",
                "tool_calls": [{"id": "1", "name": "get_weather", "arguments": {"city": "Halifax"}}],
            },
            "stopReason": "EndTurn",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }).encode()
        proc = MagicMock()
        proc.communicate.return_value = (stdout, b"")
        proc.returncode = 0
        proc.pid = 3
        with (
            patch("subscription_runner.grok_adapter.shutil.which", return_value="/usr/bin/grok"),
            patch("subscription_runner.grok_adapter.subprocess.Popen", return_value=proc) as popen,
        ):
            result = grok_adapter({
                "provider": "grok",
                "model": "grok-4.5",
                "surface": "chat",
                "prompt": "weather?",
                "tools": [{"name": "get_weather", "description": "wx", "input_schema": {}}],
            })
        self.assertEqual(result["tool_calls"][0]["name"], "get_weather")
        self.assertIsNone(result["text"])
        argv = popen.call_args[0][0]
        self.assertNotIn("--json-schema", argv)

    def test_parse_structured_output_cli_wrapper(self):
        raw = json.dumps({
            "text": "{\"type\":\"tool_calls\",\"tool_calls\":[{\"id\":\"1\",\"name\":\"get_weather\",\"arguments\":{\"when\":\"today\"}}]}",
            "structuredOutput": {
                "type": "tool_calls",
                "tool_calls": [{"id": "1", "name": "get_weather", "arguments": {"when": "today"}}],
            },
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })
        parsed = _parse_json_output(raw, expect_envelope=True)
        self.assertEqual(parsed["tool_calls"][0]["name"], "get_weather")
        self.assertIsNone(parsed["text"])

    def test_output_schema_passes_json_schema_flag(self):
        stdout = json.dumps({"text": "{\"ok\":true}", "usage": {}}).encode()
        proc = MagicMock()
        proc.communicate.return_value = (stdout, b"")
        proc.returncode = 0
        proc.pid = 4
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        with (
            patch("subscription_runner.grok_adapter.shutil.which", return_value="/usr/bin/grok"),
            patch("subscription_runner.grok_adapter.subprocess.Popen", return_value=proc) as popen,
        ):
            grok_adapter({
                "provider": "grok",
                "model": "grok-4.5",
                "surface": "worker",
                "prompt": "structured",
                "output_schema": schema,
            })
        argv = popen.call_args[0][0]
        self.assertIn("--json-schema", argv)

    def test_parse_json_string_in_text_when_no_structured_output(self):
        inner = json.dumps({
            "type": "final",
            "text": "Sunny in Halifax.",
        })
        raw = json.dumps({"text": inner, "usage": {"input_tokens": 3, "output_tokens": 1}})
        parsed = _parse_json_output(raw, expect_envelope=True)
        self.assertEqual(parsed["text"], "Sunny in Halifax.")
        self.assertEqual(parsed["tool_calls"], [])

    def test_parse_prose_prefixed_tool_calls_in_text(self):
        inner = json.dumps({
            "type": "tool_calls",
            "tool_calls": [{"name": "get_todays_events", "arguments": {}}],
        })
        raw = json.dumps({
            "text": f"I'll fetch the calendar now.{inner}",
            "usage": {"input_tokens": 3, "output_tokens": 1},
        })
        parsed = _parse_json_output(raw, expect_envelope=True)
        self.assertIsNone(parsed.get("text"))
        self.assertEqual(parsed["tool_calls"][0]["name"], "get_todays_events")

    def test_parse_structured_output_error_triggers_schema(self):
        raw = json.dumps({
            "text": "",
            "stopReason": "Cancelled",
            "structuredOutput": None,
            "structuredOutputError": "model did not produce structured output",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })
        parsed = _parse_json_output(raw, expect_envelope=True)
        self.assertEqual(parsed["error"]["code"], "schema")
        self.assertTrue(parsed["error"]["retryable"])

    def test_prose_reply_triggers_repair(self):
        prose = MagicMock()
        prose.communicate.return_value = (
            json.dumps({"text": "I'll check the weather.", "usage": {}}).encode(), b"",
        )
        prose.returncode = 0
        prose.pid = 1
        fixed = MagicMock()
        fixed.communicate.return_value = (json.dumps({
            "text": '```json\n{"type":"tool_calls","tool_calls":[{"name":"get_weather","arguments":{"when":"today"}}]}\n```',
            "usage": {},
        }).encode(), b"")
        fixed.returncode = 0
        fixed.pid = 2
        with (
            patch("subscription_runner.grok_adapter.shutil.which", return_value="/usr/bin/grok"),
            patch("subscription_runner.grok_adapter.subprocess.Popen", side_effect=[prose, fixed]),
        ):
            result = grok_adapter({
                "provider": "grok",
                "model": "grok-4.5",
                "surface": "chat",
                "prompt": "weather?",
                "tools": [{"name": "get_weather", "description": "wx", "input_schema": {}}],
            })
        self.assertEqual(result["tool_calls"][0]["name"], "get_weather")

    def test_invalid_tool_envelope_schema_then_repair(self):
        bad = MagicMock()
        bad.communicate.return_value = (json.dumps({"type": "tool_calls", "tool_calls": []}).encode(), b"")
        bad.returncode = 0
        bad.pid = 1
        good = MagicMock()
        good.communicate.return_value = (json.dumps({
            "type": "final", "text": "done",
        }).encode(), b"")
        good.returncode = 0
        good.pid = 2
        with (
            patch("subscription_runner.grok_adapter.shutil.which", return_value="/usr/bin/grok"),
            patch(
                "subscription_runner.grok_adapter.subprocess.Popen",
                side_effect=[bad, good],
            ),
        ):
            result = grok_adapter({
                "provider": "grok",
                "model": "grok-4.5",
                "surface": "chat",
                "prompt": "x",
                "tools": [{"name": "t", "input_schema": {}}],
            })
        self.assertEqual(result.get("text"), "done")

    def test_cancellation_kills_process_group(self):
        cancelled = threading.Event()
        cancelled.set()
        proc = MagicMock(returncode=-signal.SIGKILL, pid=88)
        proc.communicate.side_effect = lambda *_args, **_kwargs: (time.sleep(0.3) or (b"", b""))
        with (
            patch("subscription_runner.grok_adapter.subprocess.Popen", return_value=proc),
            patch("subscription_runner.grok_adapter.os.killpg") as killpg,
        ):
            result = run_grok_cli(
                "grok-4.5", "x", executable="/usr/bin/grok", cancel_event=cancelled,
            )
        self.assertEqual(result["error"]["message"], "Grok request cancelled")
        killpg.assert_called_once_with(88, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
