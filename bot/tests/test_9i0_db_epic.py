"""family-bot-9i0: DB locks & background fairness (29q, 995, joj, t7h, mb6, 6wl)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _find(*parts: str) -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        p = parent.joinpath(*parts)
        if p.exists():
            return p
        p2 = parent.joinpath("bot", *parts)
        if p2.exists():
            return p2
    return None


class Test29qUxRpcBudget(unittest.TestCase):
    def test_retry_budget_ux_shorter(self):
        from db_client import (
            _RPC_MAX_ATTEMPTS,
            _RPC_UX_MAX_ATTEMPTS,
            _RPC_BEST_EFFORT_MAX_ATTEMPTS,
            _retry_budget,
        )

        self.assertLess(_RPC_UX_MAX_ATTEMPTS, _RPC_MAX_ATTEMPTS)
        self.assertLess(_RPC_BEST_EFFORT_MAX_ATTEMPTS, _RPC_UX_MAX_ATTEMPTS)
        ux_n, _ = _retry_budget("update_presence", required=True, rpc_class=None)
        self.assertEqual(ux_n, _RPC_UX_MAX_ATTEMPTS)
        def_n, _ = _retry_budget("create_automation", required=True, rpc_class=None)
        self.assertEqual(def_n, _RPC_MAX_ATTEMPTS)
        batch_n, _ = _retry_budget("log_activity", required=False, rpc_class=None)
        self.assertEqual(batch_n, _RPC_BEST_EFFORT_MAX_ATTEMPTS)

    def test_best_effort_forces_batch(self):
        import inspect
        from db_client import cognition_db_write_best_effort

        src = inspect.getsource(cognition_db_write_best_effort)
        self.assertIn('rpc_class="batch"', src)

    def test_domain_priority_not_swallowed(self):
        """rpc_class must not steal create_task(priority=...) kwargs."""
        import inspect
        from db_client import cognition_db_write
        sig = inspect.signature(cognition_db_write)
        self.assertIn("rpc_class", sig.parameters)
        self.assertNotIn("priority", sig.parameters)  # domain field stays in **kwargs


class Test995LightHeavy(unittest.TestCase):
    def test_worker_source_light_heavy(self):
        p = _find("worker.py")
        self.assertIsNotNone(p)
        src = p.read_text(encoding="utf-8")
        self.assertIn("family-bot-995", src)
        self.assertIn("HEAVY_TASK_TYPES", src)
        self.assertIn("LIGHT_SLOTS", src)
        self.assertIn("exclude_types", src)
        self.assertIn("include_types", src)
        self.assertIn("queue_wait", src)

    def test_claim_accepts_filters(self):
        p = _find("database", "cognitive.py")
        self.assertIsNotNone(p)
        src = p.read_text(encoding="utf-8")
        self.assertIn("include_types", src)
        self.assertIn("exclude_types", src)
        self.assertIn("family-bot-995", src)


class TestJojIndexes(unittest.TestCase):
    def test_schema_has_joj_indexes(self):
        p = _find("database", "schema.py")
        self.assertIsNotNone(p)
        src = p.read_text(encoding="utf-8")
        self.assertIn("idx_memory_events_person_logged", src)
        self.assertIn("idx_conversation_channel_created", src)
        self.assertIn("idx_cognitive_tasks_claim", src)
        self.assertIn("family-bot-joj", src)
        self.assertIn("family-bot-6wl", src)
        # no double token_usage index in the automations block
        self.assertEqual(src.count("CREATE INDEX IF NOT EXISTS idx_token_usage_logged_at"), 1)


class TestT7hLimits(unittest.TestCase):
    def test_drafts_limited(self):
        p = _find("database", "drafts.py")
        self.assertIsNotNone(p)
        src = p.read_text(encoding="utf-8")
        self.assertIn("LIMIT ?", src)
        self.assertIn("family-bot-t7h", src)

    def test_history_range_limited(self):
        p = _find("database", "activity.py")
        self.assertIsNotNone(p)
        src = p.read_text(encoding="utf-8")
        self.assertIn("family-bot-t7h", src)
        # range query has LIMIT
        idx = src.find("async def conversation_history_in_range")
        block = src[idx : idx + 800]
        self.assertIn("LIMIT ?", block)


class TestMb6KeysStatus(unittest.TestCase):
    def test_gather_in_keys_status(self):
        p = _find("api", "routes", "activity.py")
        self.assertIsNotNone(p)
        src = p.read_text(encoding="utf-8")
        self.assertIn("family-bot-mb6", src)
        self.assertIn("asyncio.gather", src)


class TestJoj1SpendRange(unittest.TestCase):
    def test_spend_uses_logged_at_range(self):
        p = _find("database", "usage.py")
        self.assertIsNotNone(p)
        src = p.read_text(encoding="utf-8")
        self.assertIn("family-bot-joj.1", src)
        # no date(logged_at) in the two spend helpers
        idx = src.find("async def get_anthropic_spend_since")
        end = src.find("async def get_or_spend", idx)
        block = src[idx:end]
        self.assertIn("logged_at >=", block)
        # SQL must not use date(logged_at); docstring may mention the old form
        self.assertNotIn("date(logged_at) >=", block)
        self.assertNotIn("AND date(logged_at)", block)
        idx2 = src.find("async def get_or_spend")
        end2 = src.find("async def get_token_last_used", idx2)
        block2 = src[idx2:end2]
        self.assertIn("logged_at >=", block2)
        self.assertNotIn("date(logged_at) >=", block2)
        self.assertNotIn("AND date(logged_at)", block2)


class Test29qFailFastBudget(unittest.TestCase):
    """Load validation (unit): UX worst-case connection retries << 72s."""

    def test_ux_wall_clock_budget_under_10s_with_fast_backoff(self):
        from db_client import _retry_budget, _RPC_UX_MAX_ATTEMPTS

        n, base = _retry_budget("update_presence", required=True, rpc_class="ux")
        # sleeps: base*1 + base*2 + ... for n-1 intervals
        sleep_s = sum(base * (i + 1) for i in range(n - 1))
        self.assertEqual(n, _RPC_UX_MAX_ATTEMPTS)
        self.assertLess(sleep_s, 10.0, f"UX backoff sum {sleep_s}s must be <10s")


if __name__ == "__main__":
    unittest.main()
