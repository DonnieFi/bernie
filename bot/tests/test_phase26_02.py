"""Tests for Phase 26-02 — ReflectionWorker + MemoryConsolidationWorker."""
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


@unittest.skipUnless(db is not None, "database not available")
class TestPhase26_02Schema(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "phase26_02_test.db")
        await db.init_db()

    async def asyncTearDown(self):
        if hasattr(db, 'close_db'):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_tomorrow_context_exists(self):
        async with sqlite_async.connect(db.DB_PATH) as conn:
            cur = await conn.execute("PRAGMA table_info(tomorrow_context)")
            cols = {row[1] for row in await cur.fetchall()}
        for required in ("for_date", "person_id", "summary", "confidence", "source_task_id"):
            self.assertIn(required, cols)

    async def test_routines_exists(self):
        async with sqlite_async.connect(db.DB_PATH) as conn:
            cur = await conn.execute("PRAGMA table_info(routines)")
            cols = {row[1] for row in await cur.fetchall()}
        for required in ("person_id", "name", "pattern_json", "confidence",
                         "last_observed_at", "times_observed"):
            self.assertIn(required, cols)

    async def test_upsert_tomorrow_context_household_and_person(self):
        await db.upsert_tomorrow_context("2026-05-15", "Household calm", confidence=0.8)
        await db.upsert_tomorrow_context("2026-05-15", "Child1 nervous about recital",
                                          person_id="child1", confidence=0.7)
        h = await db.get_tomorrow_context("2026-05-15", person_id=None)
        c = await db.get_tomorrow_context("2026-05-15", person_id="child1")
        self.assertIn("Household", h["summary"])
        self.assertIn("recital", c["summary"])

    async def test_upsert_tomorrow_context_replaces_on_conflict(self):
        await db.upsert_tomorrow_context("2026-05-15", "v1")
        await db.upsert_tomorrow_context("2026-05-15", "v2")
        h = await db.get_tomorrow_context("2026-05-15", person_id=None)
        self.assertEqual(h["summary"], "v2")

    async def test_upsert_routine_reinforces_on_conflict(self):
        await db.upsert_routine("child1", "Monday piano", {"trigger": "Mon 7am"}, confidence=0.7)
        await db.upsert_routine("child1", "Monday piano", {"trigger": "Mon 7am"}, confidence=0.5)
        async with sqlite_async.connect(db.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = await conn.execute(
                "SELECT confidence, times_observed FROM routines WHERE person_id='child1' AND name='Monday piano'"
            )
            row = await cur.fetchone()
        self.assertAlmostEqual(row["confidence"], 0.8, places=3)   # 0.7 + 0.1 reinforcement bump
        self.assertEqual(row["times_observed"], 2)

    async def test_decay_routines_only_stale(self):
        await db.upsert_routine("child1", "fresh", {}, confidence=0.8)
        await db.upsert_routine("child1", "old", {}, confidence=0.8)
        async with sqlite_async.connect(db.DB_PATH) as conn:
            await conn.execute(
                "UPDATE routines SET last_observed_at = datetime('now','-30 days') WHERE name='old'"
            )
            await conn.commit()

        n = await db.decay_routines(decay_per_run=0.05)
        self.assertEqual(n, 1)

        async with sqlite_async.connect(db.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = await conn.execute("SELECT name, confidence FROM routines")
            rows = {r["name"]: r["confidence"] for r in await cur.fetchall()}
        self.assertAlmostEqual(rows["fresh"], 0.8, places=3)
        self.assertAlmostEqual(rows["old"], 0.75, places=3)


class TestReflectionWorkerParsing(unittest.TestCase):
    """Phase 28.5 §2: ReflectionWorker now delegates parsing to
    agent_utils.parse_typed against the ReflectionNotes Pydantic model.
    The old _parse_output method is gone; these tests assert the worker
    contract (typed output round-trip) rather than re-testing the parser
    (which has its own dedicated test module test_agent_utils.py)."""

    def test_typed_round_trip_clean_json(self):
        from agent_utils import parse_typed
        from typed_outputs import ReflectionNotes
        raw = '{"household_summary":"Quiet day","per_person":{"child1":"recital prep"},"confidence":0.78}'
        n = parse_typed(raw, ReflectionNotes)
        self.assertIsNotNone(n)
        self.assertAlmostEqual(n.confidence, 0.78)
        self.assertIn("child1", n.per_person)

    def test_typed_round_trip_markdown_fence(self):
        from agent_utils import parse_typed
        from typed_outputs import ReflectionNotes
        raw = '```json\n{"household_summary":"X","per_person":{},"confidence":0.5}\n```'
        n = parse_typed(raw, ReflectionNotes)
        self.assertIsNotNone(n)
        self.assertEqual(n.household_summary, "X")

    def test_typed_round_trip_garbage_returns_none(self):
        # Caller (ReflectionWorker.handle) detects None and retries with feedback.
        # Previously _parse_output silently emitted a confidence=0.0 dict, which
        # masked the failure — see plan §2.
        from agent_utils import parse_typed
        from typed_outputs import ReflectionNotes
        self.assertIsNone(parse_typed("not json at all just prose", ReflectionNotes))


class TestReflectionWorkerHandle(unittest.IsolatedAsyncioTestCase):
    """End-to-end handle() with mocked Ollama writes to tomorrow_context."""

    def _mock_container(self):
        from unittest.mock import MagicMock
        c = MagicMock()
        c.db = db
        return c

    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "ref_handle.db")
        await db.init_db()

    async def asyncTearDown(self):
        if hasattr(db, 'close_db'):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_handle_writes_household_and_per_person_rows(self):
        from unittest.mock import patch, AsyncMock
        from cognitive_workers.reflection import ReflectionWorker

        cfg = {
            "ollama_base_url": "http://x",
            "cognitive_workers": {"reflection": {
                "default_model": "hermes3:8b-llama3.1-q6_K",
                "upgrade_model": None,
                "num_ctx": 8192, "max_runtime_s": 60,
            }},
        }
        # Mock the Ollama call to return a structured response
        fake_json = '{"household_summary":"Quiet Friday","per_person":{"child1":"focus before recital"},"confidence":0.7}'
        fake_stats = {"model": "hermes3", "tokens_in": 200, "tokens_out": 50, "duration_ms": 800, "gpu_ms": 600}

        task_id = await db.create_cognitive_task(
            type="reflection", payload={"for_date": "2026-05-15"}, priority=10
        )
        task = {"id": task_id, "type": "reflection",
                "payload": {"for_date": "2026-05-15"}}

        with patch("worker._call_ollama_topic",
                   new=AsyncMock(return_value=(fake_json, fake_stats))):
            result = await ReflectionWorker(cfg).handle(task, self._mock_container())

        self.assertEqual(result["_stats"]["model"], "hermes3")
        self.assertEqual(result["_result"]["rows"], 2)

        h = await db.get_tomorrow_context("2026-05-15", person_id=None)
        c = await db.get_tomorrow_context("2026-05-15", person_id="child1")
        self.assertIn("Quiet Friday", h["summary"])
        self.assertIn("recital", c["summary"])

    async def test_handle_retries_on_validation_failure(self):
        """Phase 28.5 §2: when first Ollama response fails Pydantic validation,
        the worker logs a warning, retries once with the error message
        appended, and uses the corrected response. Token / cost stats from
        both calls are summed so the cognitive_tasks accounting stays honest."""
        from unittest.mock import patch, AsyncMock
        from cognitive_workers.reflection import ReflectionWorker

        cfg = {
            "ollama_base_url": "http://x",
            "cognitive_workers": {"reflection": {
                "default_model": "hermes3:8b-llama3.1-q6_K",
                "upgrade_model": None,
                "num_ctx": 8192, "max_runtime_s": 60,
            }},
        }
        # First call: invalid (confidence > 1 fails Pydantic ge=0 le=1).
        # Second call (after retry feedback): valid.
        bad_json = '{"household_summary":"x","per_person":{},"confidence":2.5}'
        good_json = '{"household_summary":"Calm Saturday","per_person":{},"confidence":0.6}'
        bad_stats = {"model": "hermes3", "tokens_in": 100, "tokens_out": 30, "duration_ms": 500, "gpu_ms": 400}
        good_stats = {"model": "hermes3", "tokens_in": 150, "tokens_out": 40, "duration_ms": 700, "gpu_ms": 500}

        task_id = await db.create_cognitive_task(
            type="reflection", payload={"for_date": "2026-05-16"}, priority=10
        )
        task = {"id": task_id, "type": "reflection",
                "payload": {"for_date": "2026-05-16"}}

        mock_call = AsyncMock(side_effect=[(bad_json, bad_stats), (good_json, good_stats)])
        with patch("worker._call_ollama_topic", new=mock_call), \
             self.assertLogs("bernie.reflection", level="WARNING") as logs:
            result = await ReflectionWorker(cfg).handle(task, self._mock_container())

        # Both calls happened.
        self.assertEqual(mock_call.await_count, 2)
        # Warning was logged about the validation failure.
        self.assertTrue(any("validation failed" in m for m in logs.output))
        # Stats merged across both calls (tokens summed).
        self.assertEqual(result["_stats"]["tokens_in"], 250)
        self.assertEqual(result["_stats"]["tokens_out"], 70)
        # Final write reflects the good (retried) response.
        h = await db.get_tomorrow_context("2026-05-16", person_id=None)
        self.assertIn("Calm Saturday", h["summary"])

    async def test_handle_emits_empty_when_retry_also_fails(self):
        """Phase 28.5 §2: if both the initial call and the retry produce
        invalid output, the worker emits an empty ReflectionNotes (confidence=0)
        and writes nothing to tomorrow_context. The OLD behaviour was a silent
        empty masquerading as a successful run — new behaviour is observable
        via a WARNING log + zero rows."""
        from unittest.mock import patch, AsyncMock
        from cognitive_workers.reflection import ReflectionWorker

        cfg = {
            "ollama_base_url": "http://x",
            "cognitive_workers": {"reflection": {
                "default_model": "hermes3:8b-llama3.1-q6_K",
                "upgrade_model": None,
                "num_ctx": 8192, "max_runtime_s": 60,
            }},
        }
        bad1 = ('this is not json', {"model": "h", "tokens_in": 50, "tokens_out": 10, "duration_ms": 100, "gpu_ms": 80})
        bad2 = ('still not json', {"model": "h", "tokens_in": 60, "tokens_out": 15, "duration_ms": 120, "gpu_ms": 90})

        task_id = await db.create_cognitive_task(
            type="reflection", payload={"for_date": "2026-05-17"}, priority=10
        )
        task = {"id": task_id, "type": "reflection",
                "payload": {"for_date": "2026-05-17"}}

        mock_call = AsyncMock(side_effect=[bad1, bad2])
        with patch("worker._call_ollama_topic", new=mock_call), \
             self.assertLogs("bernie.reflection", level="WARNING") as logs:
            result = await ReflectionWorker(cfg).handle(task, self._mock_container())

        self.assertEqual(mock_call.await_count, 2)
        self.assertEqual(result["_result"]["rows"], 0)
        # Two warnings: initial failure + post-retry failure.
        self.assertTrue(any("validation failed" in m for m in logs.output))
        self.assertTrue(any("emitting empty" in m for m in logs.output))
        # Nothing written to tomorrow_context.
        h = await db.get_tomorrow_context("2026-05-17", person_id=None)
        self.assertIsNone(h)


class TestDailySummaryPrefixWiring(unittest.TestCase):
    """Source-level checks: signature accepts prefix; daily_summary passes it."""

    def test_build_summary_embed_signature_has_prefix(self):
        import inspect
        # Read bot.py source directly — importing the module risks pulling in
        # heavy Bernie deps; we only need to confirm the function shape.
        src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
        self.assertIn("def build_summary_embed(events: list[dict], weather: dict | None = None, garbage: dict | None = None, prefix: str | None = None)",
                      src)

    def test_daily_summary_passes_prefix(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
        # The call site should fetch tomorrow_context and pass it as prefix
        self.assertIn("ctx_row = await get_database().get_tomorrow_context", src)
        self.assertIn("build_summary_embed(events, weather, garbage, prefix=ctx_prefix)", src)


class TestConsolidationWorkerParsing(unittest.TestCase):
    """Phase 28.5 §3: parsing now goes through agent_utils.parse_typed
    against ConsolidationOutput. The old _parse_output is gone; these tests
    assert the typed round-trip works end-to-end with realistic inputs.
    Parser edge cases (fences, balanced-brace extraction, validation
    failures) live in test_agent_utils.py."""

    def test_typed_round_trip_full_output(self):
        from agent_utils import parse_typed
        from typed_outputs import ConsolidationOutput
        raw = """{
          "new_routines": [{"name":"Mon piano","pattern":{},"confidence":0.8}],
          "reinforced": [{"name":"Wed soccer","confidence_bump":0.1}],
          "preference_updates": [{"key":"wake_time","value":"6:30","confidence":0.9}],
          "observations": [{"text":"Likes pour-over","confidence":0.85}]
        }"""
        c = parse_typed(raw, ConsolidationOutput)
        self.assertIsNotNone(c)
        self.assertEqual(len(c.new_routines), 1)
        self.assertAlmostEqual(c.preference_updates[0].confidence, 0.9)

    def test_typed_round_trip_empty_object_uses_defaults(self):
        from agent_utils import parse_typed
        from typed_outputs import ConsolidationOutput
        c = parse_typed('{}', ConsolidationOutput)
        self.assertIsNotNone(c)
        self.assertEqual(c.new_routines, [])
        self.assertEqual(c.reinforced, [])
        self.assertEqual(c.preference_updates, [])
        self.assertEqual(c.observations, [])

    def test_typed_round_trip_bad_confidence_rejected(self):
        # Confidence > 1 in a nested routine: the whole parse returns None
        # (not silently truncated). Caller's retry-with-feedback path picks
        # this up. Was previously a json.loads success → silent skip.
        from agent_utils import parse_typed
        from typed_outputs import ConsolidationOutput
        raw = '{"new_routines":[{"name":"x","pattern":{},"confidence":2.5}]}'
        self.assertIsNone(parse_typed(raw, ConsolidationOutput))


class TestConsolidationPersistence(unittest.IsolatedAsyncioTestCase):
    """_persist must respect min_confidence thresholds."""

    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "consol.db")
        await db.init_db()

    async def asyncTearDown(self):
        if hasattr(db, 'close_db'):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_routines_below_threshold_skipped(self):
        from cognitive_workers.consolidation import MemoryConsolidationWorker
        from typed_outputs import ConsolidationOutput
        cfg = {"cognitive_workers": {"consolidation": {
            "min_confidence_to_persist": 0.6,
            "min_preference_confidence": 0.7,
        }}}
        w = MemoryConsolidationWorker(cfg)
        # Phase 28.5 §3: _persist now consumes a typed ConsolidationOutput
        # (was: a plain dict). Pydantic validates the 0..1 confidence range
        # at construction so bad rows never reach _persist at all.
        parsed = ConsolidationOutput.model_validate({
            "new_routines": [
                {"name": "high", "pattern": {"x": 1}, "confidence": 0.8},
                {"name": "low",  "pattern": {"x": 2}, "confidence": 0.4},
            ],
        })
        counts = await w._persist("child1", parsed, db)
        self.assertEqual(counts["routines"], 1)
        self.assertEqual(counts["skipped_low_conf"], 1)
        async with sqlite_async.connect(db.DB_PATH) as conn:
            cur = await conn.execute("SELECT name FROM routines WHERE person_id='child1' ORDER BY name")
            names = [r[0] for r in await cur.fetchall()]
        self.assertEqual(names, ["high"])

    async def test_one_off_routines_rejected(self):
        from cognitive_workers.consolidation import MemoryConsolidationWorker
        from typed_outputs import ConsolidationOutput
        cfg = {"cognitive_workers": {"consolidation": {"min_confidence_to_persist": 0.6}}}
        w = MemoryConsolidationWorker(cfg)
        parsed = ConsolidationOutput.model_validate({
            "new_routines": [
                {"name": "Child1's band concert", "pattern": {}, "confidence": 0.85},
                {"name": "Mon piano", "pattern": {"day": "mon"}, "confidence": 0.8},
            ],
        })
        counts = await w._persist("child1", parsed, db)
        self.assertEqual(counts["routines"], 1)
        self.assertEqual(counts["skipped_low_conf"], 1)
        async with sqlite_async.connect(db.DB_PATH) as conn:
            cur = await conn.execute("SELECT name FROM routines WHERE person_id='child1'")
            names = [r[0] for r in await cur.fetchall()]
        self.assertEqual(names, ["Mon piano"])

    async def test_preferences_go_to_observations(self):
        from cognitive_workers.consolidation import MemoryConsolidationWorker
        from typed_outputs import ConsolidationOutput
        cfg = {"cognitive_workers": {"consolidation": {"min_preference_confidence": 0.7}}}
        w = MemoryConsolidationWorker(cfg)
        parsed = ConsolidationOutput.model_validate({
            "preference_updates": [{"key": "wake_time", "value": "6:30", "confidence": 0.9}],
        })
        counts = await w._persist("dad", parsed, db)
        self.assertEqual(counts["preferences"], 1)
        obs = await db.get_observations(person_id="dad", limit=5)
        self.assertTrue(any("preference: wake_time = 6:30" in o["observation"] for o in obs))


class TestConsolidationWorkerHandle(unittest.IsolatedAsyncioTestCase):
    """End-to-end handle() with mocked Ollama — Phase 28.5 §3.

    Mirrors TestReflectionWorkerHandle: success / retry-then-success / both-fail.
    Distinct fixture sets because MemoryConsolidationWorker has its own _persist
    threshold rules and writes to routines + observations rather than tomorrow_context."""

    def _mock_container(self):
        from unittest.mock import MagicMock
        c = MagicMock()
        c.db = db
        return c

    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "consol_handle.db")
        await db.init_db()

    async def asyncTearDown(self):
        if hasattr(db, 'close_db'):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    def _cfg(self):
        return {
            "ollama_base_url": "http://x",
            "cognitive_workers": {"consolidation": {
                "default_model": "hermes3:8b-llama3.1-q6_K",
                "upgrade_model": None,
                "num_ctx": 8192, "max_runtime_s": 60,
                "min_confidence_to_persist": 0.6,
                "min_preference_confidence": 0.7,
            }},
        }

    async def test_handle_persists_routines_and_preferences(self):
        from unittest.mock import patch, AsyncMock
        from cognitive_workers.consolidation import MemoryConsolidationWorker

        good_json = (
            '{"new_routines":[{"name":"Mon piano","pattern":{"day":"mon"},"confidence":0.8}],'
            '"preference_updates":[{"key":"wake_time","value":"6:30","confidence":0.9}],'
            '"observations":[{"text":"loves quiet mornings","confidence":0.7}]}'
        )
        good_stats = {"model": "hermes3", "tokens_in": 300, "tokens_out": 80,
                      "duration_ms": 1200, "gpu_ms": 900}
        task = {"id": 1, "type": "consolidation", "payload": {"person_id": "child1"}}

        with patch("worker._call_ollama_topic",
                   new=AsyncMock(return_value=(good_json, good_stats))):
            result = await MemoryConsolidationWorker(self._cfg()).handle(task, self._mock_container())

        self.assertEqual(result["_result"]["routines"], 1)
        self.assertEqual(result["_result"]["preferences"], 1)
        self.assertEqual(result["_result"]["observations"], 1)
        self.assertEqual(result["_stats"]["tokens_in"], 300)

    async def test_handle_retries_on_validation_failure(self):
        """Phase 28.5 §3: bad confidence → retry-with-feedback → corrected
        output drives the persist. Token stats from both calls are summed
        so the cognitive_tasks cost accounting stays honest under retry."""
        from unittest.mock import patch, AsyncMock
        from cognitive_workers.consolidation import MemoryConsolidationWorker

        # First: confidence > 1 fails Pydantic validation.
        bad = (
            '{"new_routines":[{"name":"x","pattern":{},"confidence":2.5}]}',
            {"model": "hermes3", "tokens_in": 100, "tokens_out": 30, "duration_ms": 500, "gpu_ms": 400},
        )
        # Retry: valid.
        good = (
            '{"new_routines":[{"name":"valid routine","pattern":{},"confidence":0.8}]}',
            {"model": "hermes3", "tokens_in": 150, "tokens_out": 40, "duration_ms": 700, "gpu_ms": 500},
        )
        task = {"id": 2, "type": "consolidation", "payload": {"person_id": "dad"}}
        mock_call = AsyncMock(side_effect=[bad, good])

        with patch("worker._call_ollama_topic", new=mock_call), \
             self.assertLogs("bernie.consolidation", level="WARNING") as logs:
            result = await MemoryConsolidationWorker(self._cfg()).handle(task, self._mock_container())

        self.assertEqual(mock_call.await_count, 2)
        self.assertTrue(any("validation failed" in m for m in logs.output))
        # Stats merged: both call's tokens summed.
        self.assertEqual(result["_stats"]["tokens_in"], 250)
        # The retried (valid) routine was persisted.
        self.assertEqual(result["_result"]["routines"], 1)

    async def test_handle_emits_empty_when_retry_also_fails(self):
        """Phase 28.5 §3: both invalid → empty ConsolidationOutput → 0 rows
        persisted. CRITICAL: the previous silent-empty behaviour meant weeks
        of bad model output looked like healthy consolidation passes that
        just had nothing to report. Now visible via two distinct WARNINGs."""
        from unittest.mock import patch, AsyncMock
        from cognitive_workers.consolidation import MemoryConsolidationWorker

        bad1 = ('garbage output', {"model": "h", "tokens_in": 50, "tokens_out": 10,
                                    "duration_ms": 100, "gpu_ms": 80})
        bad2 = ('still garbage', {"model": "h", "tokens_in": 60, "tokens_out": 15,
                                    "duration_ms": 120, "gpu_ms": 90})
        task = {"id": 3, "type": "consolidation", "payload": {"person_id": "dad"}}
        mock_call = AsyncMock(side_effect=[bad1, bad2])

        with patch("worker._call_ollama_topic", new=mock_call), \
             self.assertLogs("bernie.consolidation", level="WARNING") as logs:
            result = await MemoryConsolidationWorker(self._cfg()).handle(task, self._mock_container())

        self.assertEqual(mock_call.await_count, 2)
        self.assertEqual(result["_result"]["routines"], 0)
        self.assertEqual(result["_result"]["preferences"], 0)
        self.assertEqual(result["_result"]["observations"], 0)
        self.assertTrue(any("validation failed" in m for m in logs.output))
        self.assertTrue(any("emitting empty" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()
