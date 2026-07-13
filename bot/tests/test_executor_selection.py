import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestExecutorSelection(unittest.TestCase):
    def test_run_loop_uses_configured_executor(self):
        try:
            from llm import pipeline
            from executor import ServiceRefs
        except Exception as exc:
            self.skipTest(f"pipeline unavailable: {exc}")

        fake_executor = MagicMock()
        fake_executor.run = AsyncMock(return_value="ok")
        refs = ServiceRefs(llm_for=MagicMock(return_value=None))

        with patch.object(pipeline, "get_executor", return_value=fake_executor) as get_executor, \
             patch.object(pipeline, "build_service_refs", return_value=refs) as build_refs:
            result = asyncio.run(
                pipeline.run_loop(
                    client=None,
                    model="claude-sonnet-4-6",
                    system="system",
                    messages=[{"role": "user", "content": "hello"}],
                    config={"timezone": "America/Halifax"},
                    cal_service=None,
                    db_module=None,
                    tz=None,
                    session=None,
                    tools=[{
                        "name": "ping",
                        "description": "ping",
                        "input_schema": {"type": "object", "properties": {}, "required": []},
                    }],
                    triggered_by="discord",
                    group="family",
                    actor_id="123",
                    base_url=None,
                    session_id="sess-1",
                    conversation_id="conv-1",
                    surface="chat",
                    person_id="person.red",
                    user_message="hello",
                )
            )

        self.assertEqual(result, "ok")
        get_executor.assert_called_once()
        build_refs.assert_called_once()
        fake_executor.run.assert_awaited_once()
        run_args = fake_executor.run.await_args.args
        self.assertEqual(run_args[1], "system")
        self.assertEqual(run_args[2][0]["name"], "ping")
        self.assertEqual(run_args[3].surface, "chat")
        self.assertEqual(run_args[3].model, "claude-sonnet-4-6")
        self.assertEqual(run_args[3].group, "family")
        self.assertEqual(run_args[3].person_id, "person.red")
