"""Tests for Phase 26-03 — StudyGuideWorker."""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.modules.setdefault("discord", MagicMock())
sys.modules.setdefault("discord.ext", MagicMock())
sys.modules.setdefault("discord.ext.tasks", MagicMock())
sys.modules.setdefault("anthropic", MagicMock())

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import sqlite_async
import sqlite3

try:
    import sqlite_async
    import database as db
except ModuleNotFoundError:
    db = None


class TestStudyDetection(unittest.TestCase):
    def test_keyword_match(self):
        from cognitive_workers.study_detection import is_study_event
        cfg = {"cognitive_workers": {"study_keywords": "test|exam|quiz|rehearsal|recital",
                                     "study_calendars": []}}
        self.assertTrue(is_study_event({"summary": "Math Exam"}, cfg))
        self.assertTrue(is_study_event({"summary": "Piano Recital"}, cfg))
        self.assertFalse(is_study_event({"summary": "Dinner with Aunt Lin"}, cfg))

    def test_calendar_allowlist(self):
        from cognitive_workers.study_detection import is_study_event
        cfg = {"cognitive_workers": {"study_keywords": "noexistkw",
                                     "study_calendars": ["child1_school"]}}
        self.assertTrue(is_study_event({"summary": "Reading", "calendar_id": "child1_school"}, cfg))
        self.assertFalse(is_study_event({"summary": "Reading", "calendar_id": "other"}, cfg))

    def test_keyword_case_insensitive(self):
        from cognitive_workers.study_detection import is_study_event
        cfg = {"cognitive_workers": {"study_keywords": "exam", "study_calendars": []}}
        self.assertTrue(is_study_event({"summary": "SCIENCE EXAM tomorrow"}, cfg))

    def test_bad_regex_does_not_crash(self):
        from cognitive_workers.study_detection import is_study_event
        cfg = {"cognitive_workers": {"study_keywords": "[unclosed", "study_calendars": []}}
        self.assertFalse(is_study_event({"summary": "anything"}, cfg))


@unittest.skipUnless(db is not None, "database not available")
class TestEnsureStudyTask(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "study.db")
        await db.init_db()

    async def asyncTearDown(self):
        if hasattr(db, 'close_db'):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_enqueue_then_skip(self):
        from cognitive_workers.study_detection import ensure_study_task
        ev = {"id": "ev1", "summary": "Math Exam", "start": "2026-05-15T08:00:00"}
        a = await ensure_study_task(ev, person_id="child1")
        b = await ensure_study_task(ev, person_id="child1")
        self.assertIsNotNone(a)
        self.assertIsNone(b, "second call must be a no-op for the same event_id")

        async with sqlite_async.connect(db.DB_PATH) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM cognitive_tasks WHERE type='study_guide'"
            )
            self.assertEqual((await cur.fetchone())[0], 1)

    async def test_dead_letter_blocks_reenqueue_within_cooldown(self):
        from cognitive_workers.study_detection import ensure_study_task
        from datetime import datetime, timezone as dt_timezone

        ev = {"id": "ev_dl", "summary": "Science Quiz", "start": "2026-05-16T08:00:00"}
        first = await ensure_study_task(ev, person_id="child1")
        self.assertIsNotNone(first)

        now = datetime.now(dt_timezone.utc).isoformat()
        async with sqlite_async.connect(db.DB_PATH) as conn:
            await conn.execute(
                """UPDATE cognitive_tasks
                   SET status='dead_letter', retry_count=3, error='test failure',
                       created_at=?, completed_at=NULL
                   WHERE id=?""",
                (now, first),
            )
            await conn.commit()

        second = await ensure_study_task(ev, person_id="child1")
        self.assertIsNone(second, "dead_letter within 24h must block study_scan re-enqueue")

        async with sqlite_async.connect(db.DB_PATH) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM cognitive_tasks WHERE type='study_guide'"
            )
            self.assertEqual((await cur.fetchone())[0], 1)


@unittest.skipUnless(db is not None, "database not available")
class TestStudyGuideHandle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "sg_handle.db")
        await db.init_db()

    async def asyncTearDown(self):
        if hasattr(db, 'close_db'):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_handle_stores_output_and_enqueues_delivery(self):
        from types import SimpleNamespace
        from unittest.mock import patch, AsyncMock
        from cognitive_workers.study_guide import StudyGuideWorker
        from datetime import datetime, timedelta, timezone as dt_timezone

        container = SimpleNamespace(db=db)

        cfg = {"ollama_base_url": "http://x",
               "cognitive_workers": {"study_guide": {
                   "default_model": "phi4", "num_ctx": 8192,
                   "max_runtime_s": 120, "dm_lead_time_hours": 2}}}
        event_start = (datetime.now(dt_timezone.utc) + timedelta(hours=4)).isoformat()
        task_id = await db.create_cognitive_task(
            type="study_guide",
            payload={"event_id": "ev1", "person_id": "child1",
                     "summary": "Math Exam", "description": "Algebra unit",
                     "start": event_start},
        )
        task = {"id": task_id, "type": "study_guide",
                "payload": {"event_id": "ev1", "person_id": "child1",
                            "summary": "Math Exam", "description": "Algebra unit",
                            "start": event_start}}
        with patch("worker._call_ollama_topic",
                   new=AsyncMock(return_value=(
                       "## Cheat Sheet\nKey concepts: linear equations",
                       {"model": "phi4", "tokens_in": 50, "tokens_out": 80,
                        "duration_ms": 1000, "gpu_ms": 800}))):
            result = await StudyGuideWorker(cfg).handle(task, container)

        self.assertIn("_stats", result)
        out = await db.get_task_output_by_key("study_guide:ev1")
        self.assertIsNotNone(out)
        self.assertIn("Cheat Sheet", out["content"])

        async with sqlite_async.connect(db.DB_PATH) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM cognitive_tasks WHERE type='study_guide_deliver'"
            )
            self.assertEqual((await cur.fetchone())[0], 1)

    async def test_delivery_run_at_two_hours_before_event(self):
        from types import SimpleNamespace
        from unittest.mock import patch, AsyncMock
        from cognitive_workers.study_guide import StudyGuideWorker
        from datetime import datetime, timedelta, timezone as dt_timezone

        container = SimpleNamespace(db=db)

        cfg = {"ollama_base_url": "http://x",
               "cognitive_workers": {"study_guide": {
                   "default_model": "phi4", "num_ctx": 4096,
                   "max_runtime_s": 60, "dm_lead_time_hours": 2}}}
        event_start = (datetime.now(dt_timezone.utc) + timedelta(hours=8)).isoformat()
        task_id = await db.create_cognitive_task(
            type="study_guide",
            payload={"event_id": "ev2", "person_id": "child1",
                     "summary": "Exam", "description": "", "start": event_start},
        )
        task = {"id": task_id, "type": "study_guide",
                "payload": {"event_id": "ev2", "person_id": "child1",
                            "summary": "Exam", "description": "", "start": event_start}}
        with patch("worker._call_ollama_topic",
                   new=AsyncMock(return_value=("ok", {
                       "model": "phi4", "tokens_in": 1, "tokens_out": 1,
                       "duration_ms": 10, "gpu_ms": 5}))):
            await StudyGuideWorker(cfg).handle(task, container)

        async with sqlite_async.connect(db.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = await conn.execute(
                "SELECT run_at FROM cognitive_tasks WHERE type='study_guide_deliver'"
            )
            row = await cur.fetchone()
        self.assertIsNotNone(row)
        run_at = datetime.fromisoformat(row["run_at"].replace("Z", "+00:00"))
        expected = datetime.now(dt_timezone.utc) + timedelta(hours=6)
        diff = abs((run_at - expected).total_seconds())
        self.assertLess(diff, 60, f"run_at off by {diff}s from 6h-from-now")


if __name__ == "__main__":
    unittest.main()
