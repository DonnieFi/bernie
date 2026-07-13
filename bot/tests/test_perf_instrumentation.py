"""Perf instrumentation tests (P0 🔬).

All new tests use stdlib unittest + IsolatedAsyncioTestCase per CLAUDE.md.

These drive the *shipped* code:
- bot.llm.turn_timer.TurnTimer (real context manager, marks, record, current())
- database public helpers log_turn_timing / log_context_build / log_llm_iteration
  (and surface on log_token_usage)

No hard-coded expected values from outside the module under test.
Temp DB + DB_PATH swap for isolation.
"""

import unittest
import asyncio
import tempfile
import sqlite3
import os
import sys
import time as _time
from unittest.mock import patch

import database as dbmod

# Exercise the *prod import path* 'from llm.turn_timer' + llm/__init__.py as
# required. When running inside the container (via ssh), 'llm' is importable.
# Locally we fall back so the test file can at least be parsed/loaded.
try:
    from llm.turn_timer import TurnTimer  # prod path
    _USING_PROD_LLM_IMPORT = True
except Exception:
    import database as _db  # ensure layout
    from llm.turn_timer import TurnTimer  # fallback (with PYTHONPATH=/app/bot this works)
    _USING_PROD_LLM_IMPORT = False


def _init_minimal_tables(db_path: str) -> None:
    # Clean any stale WAL/sidecar files that can cause "readonly" or lock with aiosqlite + WAL
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            os.unlink(db_path + suffix)
        except OSError:
            pass
    try:
        os.chmod(db_path, 0o666)
    except OSError:
        pass
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            event_type TEXT NOT NULL,
            description TEXT,
            person_id TEXT,
            metadata TEXT
        );
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            input_tokens INTEGER,
            output_tokens INTEGER,
            model TEXT,
            conversation_id TEXT,
            triggered_by TEXT,
            surface TEXT DEFAULT 'discord',
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            session_id TEXT
        );
    """)
    conn.commit()
    conn.close()


class TestTurnTimerAndLogs(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_role = os.environ.get("ROLE")
        os.environ["ROLE"] = "monolith"
        # Isolate to a temp DB file; database module supports DB_PATH swap.
        self._orig_db_path = dbmod.DB_PATH
        fd, self._tmp_db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        # Ensure writable and no stale sidecars from previous partial runs (aiosqlite + WAL is picky)
        try:
            os.chmod(self._tmp_db, 0o666)
        except OSError:
            pass
        for s in ("-wal", "-shm", "-journal"):
            try:
                os.unlink(self._tmp_db + s)
            except OSError:
                pass
        dbmod.DB_PATH = self._tmp_db
        # Force the singleton to re-open against the new path
        dbmod._conn = None
        dbmod._conn_path = None
        _init_minimal_tables(self._tmp_db)

    async def asyncTearDown(self):
        if self._old_role is None:
            os.environ.pop("ROLE", None)
        else:
            os.environ["ROLE"] = self._old_role
        dbmod.DB_PATH = self._orig_db_path
        dbmod._conn = None
        dbmod._conn_path = None
        try:
            os.unlink(self._tmp_db)
        except Exception:
            pass

    async def test_turn_timer_records_phases_and_logs_via_public_api(self):
        """Drive the real TurnTimer + public log_turn_timing path using only mark() + real elapsed."""
        turn_id = "test-turn-001"
        phases_seen = {}

        # real elapsed, only mark
        async with TurnTimer(turn_id=turn_id, channel_id="123", person_id="child1") as tt:
            _time.sleep(0.005)
            tt.mark("setup")
            _time.sleep(0.003)
            tt.mark("context")
            _time.sleep(0.004)
            tt.mark("llm")
            _time.sleep(0.002)
            tt.mark("tools")
            _time.sleep(0.001)
            tt.mark("send")

            phases_seen = dict(tt.phases)

        if hasattr(tt, '_log_task') and tt._log_task:
            try:
                await tt._log_task
            except Exception:
                pass

        # Assert phases from shipped timer
        self.assertIn("setup", phases_seen)
        self.assertIn("context", phases_seen)
        self.assertIn("llm", phases_seen)
        self.assertIn("tools", phases_seen)
        self.assertIn("send", phases_seen)

        # row from real path
        conn = sqlite3.connect(self._tmp_db)
        row = conn.execute(
            "SELECT event_type, description, metadata FROM activity_log "
            "WHERE event_type = 'turn_timing' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "turn_timing row must be written by TurnTimer.__aexit__ using shipped log helper")
        self.assertEqual(row[0], "turn_timing")
        self.assertIn(turn_id, (row[1] or "") + str(row[2] or ""))
        import json
        outer = json.loads(row[2] or '{}') if row and row[2] else {}
        inner = json.loads(outer.get('meta', '{}')) if outer.get('meta') else outer
        self.assertGreater(inner.get('total_ms', 0), 0, "total_ms must be >0 from real monotonic marks in TurnTimer")
        self.assertEqual(inner.get('context_ms'), phases_seen.get('context'))
        self.assertEqual(inner.get('tools_ms'), phases_seen.get('tools'))

    async def test_turn_timer_current_is_available_inside_context(self):
        """current() must return the active timer so deep code can record/c correlate."""
        seen = []

        async with TurnTimer(turn_id="cur-42") as tt:
            cur = TurnTimer.current()
            seen.append(cur is tt)
            if cur:
                cur.record("deep_phase", 7)

        self.assertTrue(seen[0], "TurnTimer.current() must yield the active instance inside the context")
        # After exit, current should be cleared
        self.assertIsNone(TurnTimer.current())

    async def test_log_helpers_write_with_surface_and_turn_id(self):
        """Drive the real public log_* helpers and verify surface + ids (for correlation)."""
        await dbmod.log_turn_timing(turn_id="t-9", total_ms=1234, context_ms=56, channel_id="smithy", person_id="dad")
        await dbmod.log_context_build(turn_id="t-9", calendar_ms=300, total_ms=450, channel_id="smithy")
        await dbmod.log_llm_iteration(turn_id="t-9", step=1, prompt_hash="deadbeef", tokens_in=18000, delta_tokens=2000)
        await dbmod.log_token_usage(100, 10, "or-glm-52", surface="shadow", triggered_by="shadow:model")

        conn = sqlite3.connect(self._tmp_db)
        act = conn.execute("SELECT event_type FROM activity_log WHERE event_type IN ('turn_timing','context_build','llm_iteration')").fetchall()
        surf = conn.execute("SELECT surface FROM token_usage WHERE surface='shadow'").fetchone()
        conn.close()

        self.assertTrue(any(r[0] == "turn_timing" for r in act))
        self.assertTrue(any(r[0] == "context_build" for r in act))
        self.assertTrue(any(r[0] == "llm_iteration" for r in act))
        self.assertIsNotNone(surf)

    async def test_log_token_usage_persists_surface_shadow(self):
        """log_token_usage must persist the surface column for shadow correlation."""
        await dbmod.log_token_usage(5000, 300, "test-model", surface="shadow")
        conn = sqlite3.connect(self._tmp_db)
        row = conn.execute("SELECT surface FROM token_usage ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        self.assertEqual(row[0], "shadow")

    async def test_smol_primary_logs_discord_and_shadow_logs_harness_surface(self):
        from executor import ExecutorConfig, ServiceRefs
        from executors.smol import _log_smol_generation

        services = ServiceRefs(db=dbmod)
        primary_cfg = ExecutorConfig(surface="chat", model="test", shadow=False, triggered_by="discord")
        shadow_cfg = ExecutorConfig(surface="chat", model="test", shadow=True, triggered_by="discord")

        with patch("langfuse_logger.log_generation"):
            await _log_smol_generation(
                config=primary_cfg,
                services=services,
                user_input="hi",
                output="hello",
                input_tokens=10,
                output_tokens=1,
            )
            await _log_smol_generation(
                config=shadow_cfg,
                services=services,
                user_input="hi",
                output="hello",
                input_tokens=10,
                output_tokens=1,
            )

        conn = sqlite3.connect(self._tmp_db)
        rows = conn.execute("SELECT surface FROM token_usage ORDER BY id DESC LIMIT 2").fetchall()
        conn.close()
        self.assertEqual([r[0] for r in rows], ["shadow_harness", "discord"])

    async def test_queue_resizes_and_sheds_shadow_when_full(self):
        from llm.queue import LLMQueue

        q = LLMQueue(max_depth=1, shed_shadow_first=True)
        async with q.slot(shadow=False):
            self.assertFalse(await q.acquire(shadow=True))

        await q.configure(max_depth=2, shed_shadow_first=True)
        async with q.slot(shadow=False):
            self.assertTrue(await q.acquire(shadow=True))
            async with q._cond:
                q._depth = max(0, q._depth - 1)
                q._cond.notify_all()

    async def test_executor_config_accepts_turn_id_for_iteration_correlation(self):
        from executor import ExecutorConfig

        cfg = ExecutorConfig(surface="chat", model="test", turn_id="turn-123")
        self.assertEqual(cfg.turn_id, "turn-123")


if __name__ == "__main__":
    unittest.main()
