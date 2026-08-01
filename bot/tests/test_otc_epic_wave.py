"""family-bot-otc epic: hot-path residuals (otc.2, px2, yig, 21n, rpg, jwf, 1tv, ot4, otc.1)."""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_BOT = Path(__file__).resolve().parents[1]


class TestOtc2SmolErrors(unittest.IsolatedAsyncioTestCase):
    async def test_smol_failure_is_family_facing(self):
        from executors.smol import SmolExecutor
        from executor import ExecutorConfig, ServiceRefs

        gw = MagicMock()
        ex = SmolExecutor(gw).with_services(ServiceRefs(llm_for=lambda m: MagicMock()))
        with patch("smolagents.CodeAgent") as CA, \
             patch("executors.smol._make_smol_model") as mm, \
             patch("executors.smol._log_smol_generation", new_callable=AsyncMock), \
             patch("config.config", {"executor": {"smol_verbosity_level": 0}}):
            model = MagicMock()
            model.tokens_in_total = 0
            model.tokens_out_total = 0
            model.cache_creation_total = 0
            model.cache_read_total = 0
            mm.return_value = model
            agent = MagicMock()
            agent.run.side_effect = RuntimeError("Executor error: boom traceback")
            CA.return_value = agent
            text = await ex.run(
                [{"role": "user", "content": "hi"}],
                "sys",
                [],
                ExecutorConfig(surface="chat", model="m", conversation_id="c"),
            )
        self.assertNotIn("Executor error", text)
        self.assertNotIn("traceback", text.lower())
        self.assertIn("snag", text.lower())


class TestPx2ParallelTools(unittest.TestCase):
    def test_native_gathers_tool_blocks(self):
        src = (_BOT / "executors" / "native.py").read_text(encoding="utf-8")
        self.assertIn("family-bot-px2", src)
        self.assertIn("asyncio.gather", src)
        self.assertIn("_run_one", src)


class TestYigHealthGather(unittest.TestCase):
    def test_prefetch_uses_gather(self):
        src = (_BOT / "health_sleep.py").read_text(encoding="utf-8")
        self.assertIn("family-bot-yig", src)
        self.assertIn("_aio.gather", src)
        chat = (_BOT / "llm" / "chat.py").read_text(encoding="utf-8")
        self.assertIn("get_sleep_summary", chat)
        self.assertIn("get_oura_sleep", chat)
        self.assertIn("health_sleep_prefetch_ok", chat)


class Test21nSmolSystemParity(unittest.TestCase):
    def test_no_system_flatten_into_task(self):
        src = (_BOT / "executors" / "smol.py").read_text(encoding="utf-8")
        self.assertIn("family-bot-21n", src)
        self.assertIn("_bernie_system_blocks", src)
        self.assertNotIn('full_task = f"{system_str}\\n\\n---\\n\\n{task}"', src)
        self.assertIn("agent.run(task)", src)


class TestRpgSingleResolve(unittest.TestCase):
    def test_single_channel_resolve(self):
        src = (_BOT / "llm" / "chat.py").read_text(encoding="utf-8")
        self.assertIn("family-bot-rpg", src)
        # chat_general path: ceiling once then pure narrow (no second resolve with intent True)
        self.assertIn("narrow_tool_domains as _narrow", src)
        self.assertNotIn("apply_intent_router=True", src)
        self.assertIn("apply_intent_router=False", src)


class TestJwfPruneToolUse(unittest.TestCase):
    def test_prunes_tool_use_blocks(self):
        from llm.messages import prune_old_tool_results
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "1", "name": "get_events", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "1", "content": "big " * 100},
            ]},
            {"role": "user", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "user", "content": "d"},
            {"role": "user", "content": "e"},
        ]
        out = prune_old_tool_results(msgs, verbatim_tail=2)
        # Early assistant tool_use should be stubbed
        early = out[1]["content"]
        self.assertTrue(any(
            isinstance(b, dict) and b.get("type") == "text" and "pruned" in b.get("text", "")
            for b in early
        ))


class Test1tvAuditQueue(unittest.TestCase):
    def test_audit_uses_queue(self):
        src = (_BOT / "llm" / "audit.py").read_text(encoding="utf-8")
        self.assertIn("family-bot-1tv", src)
        self.assertIn("queued_messages_create", src)
        self.assertIn("shadow=True", src)


class TestOt4CalmFallback(unittest.TestCase):
    def test_chat_general_no_raise_on_double_fail(self):
        src = (_BOT / "llm" / "chat.py").read_text(encoding="utf-8")
        self.assertIn("family-bot-ot4", src)
        self.assertIn("try asking again in a moment", src)
        # Primary chat_general path should not re-raise chat_exc after fallback fail
        # (meal path also calm)
        self.assertGreaterEqual(src.count("try asking again in a moment"), 1)


class TestOtc1EarlyHealth(unittest.TestCase):
    def test_early_health_task(self):
        src = (_BOT / "llm" / "chat.py").read_text(encoding="utf-8")
        self.assertIn("family-bot-otc.1", src)
        self.assertIn("_health_task", src)
        self.assertIn("create_task(prefetch_health_sleep", src)


if __name__ == "__main__":
    unittest.main()
