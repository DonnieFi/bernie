"""Tests for Phase 26-01 — Ollama hardening + cost rails.

Project convention: unittest.IsolatedAsyncioTestCase (NOT pytest).
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

# Mock the heavy third-party modules Bernie imports — same pattern as test_phase24.py
sys.modules.setdefault("discord", MagicMock())
sys.modules.setdefault("discord.ext", MagicMock())
sys.modules.setdefault("discord.ext.tasks", MagicMock())
sys.modules.setdefault("anthropic", MagicMock())

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import database as db
except ModuleNotFoundError:
    db = None

import sqlite_async
import sqlite3


@unittest.skipUnless(db is not None, "database not available in this test environment")
class TestCognitiveTasksSchema(unittest.IsolatedAsyncioTestCase):
    """cognitive_tasks must expose model_used / tokens_in / tokens_out / duration_ms / gpu_ms."""

    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "phase26_01_test.db")
        await db.init_db()

    async def asyncTearDown(self):
        if hasattr(db, 'close_db'):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_cost_columns_exist(self):
        async with sqlite_async.connect(db.DB_PATH) as conn:
            cur = await conn.execute("PRAGMA table_info(cognitive_tasks)")
            cols = {row[1] for row in await cur.fetchall()}
        for required in ("model_used", "tokens_in", "tokens_out", "duration_ms", "gpu_ms"):
            self.assertIn(required, cols, f"missing column: {required}")


class TestOllamaCallWithStats(unittest.IsolatedAsyncioTestCase):
    """_call_ollama_topic must accept num_ctx and return (text, stats) tuple."""

    async def test_num_ctx_and_stats_returned(self):
        from unittest.mock import patch, AsyncMock, MagicMock

        fake_body = {
            "message": {"content": "hello"},
            "prompt_eval_count": 12,
            "eval_count": 34,
            "total_duration": 5_000_000_000,   # 5s in ns
            "eval_duration": 3_000_000_000,    # 3s in ns
            "model": "qwen2.5:14b",
        }

        # Mock aiohttp.ClientSession with a context-manager-friendly chain
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value=fake_body)
        post_cm = MagicMock()
        post_cm.__aenter__ = AsyncMock(return_value=resp)
        post_cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.post = MagicMock(return_value=post_cm)
        sess_cm = MagicMock()
        sess_cm.__aenter__ = AsyncMock(return_value=session)
        sess_cm.__aexit__ = AsyncMock(return_value=None)

        import worker
        with patch("cognitive_handlers.worker_shared.aiohttp.ClientSession", return_value=sess_cm):
            text, stats = await worker._call_ollama_topic(
                "qwen2.5:14b", "hi", {"ollama_base_url": "http://x"}, num_ctx=8192
            )

        self.assertEqual(text, "hello")
        self.assertEqual(stats["model"], "qwen2.5:14b")
        self.assertEqual(stats["tokens_in"], 12)
        self.assertEqual(stats["tokens_out"], 34)
        self.assertEqual(stats["duration_ms"], 5000)
        self.assertEqual(stats["gpu_ms"], 3000)

        # And verify num_ctx was passed in the options
        called_kwargs = session.post.call_args.kwargs
        self.assertEqual(called_kwargs["json"]["options"]["num_ctx"], 8192)


class TestOllamaSemaphore(unittest.IsolatedAsyncioTestCase):
    """OLLAMA_SEMAPHORE must exist at module level and have a single permit."""

    def test_semaphore_exists_with_size_one(self):
        import worker
        self.assertTrue(hasattr(worker, "OLLAMA_SEMAPHORE"),
                        "worker.py must expose OLLAMA_SEMAPHORE at module level")
        # asyncio.Semaphore exposes its current available count via _value
        self.assertEqual(worker.OLLAMA_SEMAPHORE._value, 1,
                         "OLLAMA_SEMAPHORE must be sized at 1 (deba VRAM protection)")

    async def test_semaphore_is_acquired_inside_call(self):
        """Quick functional check: while a call is in flight, semaphore._value goes to 0."""
        import asyncio
        from unittest.mock import patch, AsyncMock, MagicMock

        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={"message": {"content": "ok"}})
        post_cm = MagicMock()
        post_cm.__aenter__ = AsyncMock(return_value=resp)
        post_cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.post = MagicMock(return_value=post_cm)
        sess_cm = MagicMock()
        sess_cm.__aenter__ = AsyncMock(return_value=session)
        sess_cm.__aexit__ = AsyncMock(return_value=None)

        import worker
        with patch("cognitive_handlers.worker_shared.aiohttp.ClientSession", return_value=sess_cm):
            # Manually acquire the semaphore — _call_ollama_topic should block on it
            await worker.OLLAMA_SEMAPHORE.acquire()
            try:
                task = asyncio.create_task(
                    worker._call_ollama_topic("m", "q", {"ollama_base_url": "http://x"})
                )
                await asyncio.sleep(0.05)
                self.assertFalse(task.done(),
                                 "Call must block while OLLAMA_SEMAPHORE is held externally")
            finally:
                worker.OLLAMA_SEMAPHORE.release()
            text, _ = await task
            self.assertEqual(text, "ok")


@unittest.skipUnless(db is not None, "database not available")
class TestCompleteWithStats(unittest.IsolatedAsyncioTestCase):
    """complete_cognitive_task_with_stats must persist all cost columns."""

    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "phase26_01_stats.db")
        await db.init_db()

    async def asyncTearDown(self):
        if hasattr(db, 'close_db'):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_complete_with_stats_writes_columns(self):
        task_id = await db.create_cognitive_task(
            type="reflection", payload={}, priority=0
        )
        await db.claim_next_task()
        await db.complete_cognitive_task_with_stats(
            task_id,
            result={"ok": True},
            stats={
                "model": "qwen2.5:14b",
                "tokens_in": 100,
                "tokens_out": 200,
                "duration_ms": 5000,
                "gpu_ms": 3000,
            },
        )
        async with sqlite_async.connect(db.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = await conn.execute(
                "SELECT model_used, tokens_in, tokens_out, duration_ms, gpu_ms, status FROM cognitive_tasks WHERE id=?",
                (task_id,)
            )
            row = await cur.fetchone()
        self.assertEqual(row["model_used"], "qwen2.5:14b")
        self.assertEqual(row["tokens_in"], 100)
        self.assertEqual(row["tokens_out"], 200)
        self.assertEqual(row["duration_ms"], 5000)
        self.assertEqual(row["gpu_ms"], 3000)
        self.assertEqual(row["status"], "done")


class TestSmallModelDiscipline(unittest.IsolatedAsyncioTestCase):
    """Every _call_ollama_topic invocation must prepend SMALL_MODEL_DISCIPLINE."""

    async def test_discipline_prepended_to_system(self):
        from unittest.mock import patch, AsyncMock, MagicMock

        captured = {}

        def make_post_cm(*args, **kwargs):
            captured["json"] = kwargs.get("json")
            resp = MagicMock()
            resp.status = 200
            resp.json = AsyncMock(return_value={"message": {"content": "ok"}})
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        session = MagicMock()
        session.post = MagicMock(side_effect=make_post_cm)
        sess_cm = MagicMock()
        sess_cm.__aenter__ = AsyncMock(return_value=session)
        sess_cm.__aexit__ = AsyncMock(return_value=None)

        import worker
        with patch("cognitive_handlers.worker_shared.aiohttp.ClientSession", return_value=sess_cm):
            await worker._call_ollama_topic(
                "m", "hi", {"ollama_base_url": "http://x"},
                system="CUSTOM WORKER SYSTEM"
            )

        sys_msg = captured["json"]["messages"][0]["content"]
        self.assertIn("Small-model rules", sys_msg)
        self.assertIn("CUSTOM WORKER SYSTEM", sys_msg)
        # Discipline must precede the worker-specific system text
        self.assertLess(
            sys_msg.index("Small-model rules"),
            sys_msg.index("CUSTOM WORKER SYSTEM"),
        )


class TestCognitiveWorkerBase(unittest.TestCase):
    """CognitiveWorkerBase escalation rule — deterministic input-token threshold."""

    def _W(self, **kwargs):
        from cognitive_workers import CognitiveWorkerBase

        class _W(CognitiveWorkerBase):
            name = "test"
            default_model = kwargs.get("default", "small")
            upgrade_model = kwargs.get("upgrade")
            escalate_above_tokens = kwargs.get("threshold", 1000)
            num_ctx = 4096
            max_runtime_s = 60

            async def handle(self, task, bot):
                return None
        return _W()

    def test_pick_model_below_threshold(self):
        w = self._W(upgrade="big", threshold=1000)
        self.assertEqual(w.pick_model(500), "small")

    def test_pick_model_above_threshold(self):
        w = self._W(upgrade="big", threshold=1000)
        self.assertEqual(w.pick_model(1500), "big")

    def test_pick_model_no_upgrade(self):
        w = self._W(upgrade=None, threshold=1000)
        self.assertEqual(w.pick_model(99999), "small")


if __name__ == "__main__":
    unittest.main()
