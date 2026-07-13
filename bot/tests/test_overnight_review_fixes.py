"""Tests for the 2026-05-22 overnight merge review fixes.

Covers:
- Tier-1 judge success doesn't advance to fallbacks (no wasted calls).
- _is_transient_judge_error catches network-shaped exception classes
  (APIConnectionError, httpx.ConnectError, etc.) that don't carry a
  status_code attribute. This is the gap the Anthropic outage exposed.
- 409 is no longer in the transient status list (drop per review #5).
- Worker retry-with-feedback escalates to upgrade_model when configured.
- db.upsert_routine honours reinforce_bump (was hard-coded +0.1).
- BERNIE_TEST_LOG pointing into /data/ is rejected at bootstrap.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _worker_container(db):
    container = MagicMock()
    container.db = db
    return container


# ── Judge fallback chain ───────────────────────────────────────────────────────

class _FakeModelHTTPError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"status_code: {status_code}")


def _make_result():
    res = MagicMock()
    res.output = MagicMock(a_intent=0.8, a_factual=0.7, b_intent=0.7, b_factual=0.6)
    res.output.model_dump_json = MagicMock(return_value="{}")
    res.usage = MagicMock(input_tokens=10, output_tokens=5)
    return res


class TestJudgeTierOneSuccess(unittest.TestCase):
    """If the primary model succeeds, no fallback tier should be invoked.
    Catches a regression where the chain pre-warms or always loops."""

    def test_primary_success_does_not_call_fallbacks(self):
        import eval_service
        call_log = []

        def fake_make(model, result_type):
            call_log.append(model)
            return MagicMock(run=AsyncMock(return_value=_make_result()))

        import config as config_mod
        fake_cfg = {"eval": {
            "judge_fallback_model": "or-deepseek-v4",
            "judge_ollama_fallback": "mistral-nemo:12b-instruct-2407-q5_K_M",
        }}
        with patch.object(eval_service, "_make_judge_agent", side_effect=fake_make), \
             patch.object(eval_service, "ANTHROPIC_KEY", "sk-fake"), \
             patch.object(config_mod, "config", fake_cfg):
            result = asyncio.run(eval_service.judge_pair("p", "s", "claude-sonnet-4-6", "u"))

        self.assertIsNotNone(result)
        self.assertEqual(call_log, ["claude-sonnet-4-6"],
                         "tier-1 success must NOT advance to fallbacks")


# ── Transient classifier ───────────────────────────────────────────────────────

class TestTransientClassifier(unittest.TestCase):
    """eval_service._is_transient_judge_error must catch network-shaped
    exception classes that lack a status_code attribute."""

    def test_api_connection_error_is_transient(self):
        from eval_service import _is_transient_judge_error

        class APIConnectionError(Exception):
            pass

        self.assertTrue(_is_transient_judge_error(APIConnectionError("anthropic down")))

    def test_httpx_connect_error_is_transient(self):
        from eval_service import _is_transient_judge_error

        class ConnectError(Exception):
            pass

        self.assertTrue(_is_transient_judge_error(ConnectError("dns")))

    def test_read_timeout_is_transient(self):
        from eval_service import _is_transient_judge_error

        class ReadTimeout(Exception):
            pass

        self.assertTrue(_is_transient_judge_error(ReadTimeout("slow")))

    def test_network_error_class_is_transient(self):
        from eval_service import _is_transient_judge_error

        class NetworkError(Exception):
            pass

        self.assertTrue(_is_transient_judge_error(NetworkError("unreachable")))

    def test_409_no_longer_transient(self):
        """Review item #5: 409 is not a typical LLM-API failure; dropped."""
        from eval_service import _is_transient_judge_error
        self.assertFalse(_is_transient_judge_error(_FakeModelHTTPError(409)))

    def test_500_still_transient(self):
        from eval_service import _is_transient_judge_error
        self.assertTrue(_is_transient_judge_error(_FakeModelHTTPError(503)))

    def test_400_still_not_transient(self):
        """Prompt errors don't get retried on a smaller model."""
        from eval_service import _is_transient_judge_error
        self.assertFalse(_is_transient_judge_error(_FakeModelHTTPError(400)))


# ── Worker retry escalation ────────────────────────────────────────────────────

class TestWorkerRetryEscalatesToUpgradeModel(unittest.IsolatedAsyncioTestCase):
    """Phase 28.5 review C: when parse fails on tier-1, the retry should
    use upgrade_model (if configured) instead of repeating the same
    underpowered model that just failed."""

    async def asyncSetUp(self):
        import tempfile, database as db
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "retry_escalate.db")
        await db.init_db()

    async def asyncTearDown(self):
        import database as db
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_retry_uses_upgrade_model_when_configured(self):
        import database as db
        from cognitive_workers.reflection import ReflectionWorker

        cfg = {
            "ollama_base_url": "http://x",
            "cognitive_workers": {"reflection": {
                "default_model": "hermes3:8b-llama3.1-q6_K",
                "upgrade_model": "qwen2.5:14b",
                "num_ctx": 8192, "max_runtime_s": 60,
            }},
        }
        bad_json = '{"household_summary":"x","per_person":{},"confidence":2.5}'
        good_json = '{"household_summary":"ok","per_person":{},"confidence":0.6}'
        stats = {"model": "h", "tokens_in": 50, "tokens_out": 10, "duration_ms": 100, "gpu_ms": 80}

        task_id = await db.create_cognitive_task(
            type="reflection", payload={"for_date": "2026-05-19"}, priority=10,
        )
        task = {"id": task_id, "type": "reflection", "payload": {"for_date": "2026-05-19"}}

        call_models = []

        async def fake_topic(model, topic, config, num_ctx=None, system=None, timeout_s=300):
            call_models.append(model)
            return (bad_json if len(call_models) == 1 else good_json, stats)

        with patch("worker._call_ollama_topic", side_effect=fake_topic):
            result = await ReflectionWorker(cfg).handle(task, container=_worker_container(db))

        self.assertEqual(call_models, [
            "hermes3:8b-llama3.1-q6_K", "qwen2.5:14b",
        ], "retry must escalate to upgrade_model, not repeat the same small model")
        self.assertEqual(result["_result"]["rows"], 1)

    async def test_retry_repeats_same_model_when_no_upgrade(self):
        """Backwards-compatible behaviour when upgrade_model is None."""
        import database as db
        from cognitive_workers.reflection import ReflectionWorker

        cfg = {
            "ollama_base_url": "http://x",
            "cognitive_workers": {"reflection": {
                "default_model": "hermes3:8b-llama3.1-q6_K",
                "upgrade_model": None,
                "num_ctx": 8192, "max_runtime_s": 60,
            }},
        }
        bad_json = '{"household_summary":"x","per_person":{},"confidence":2.5}'
        good_json = '{"household_summary":"ok","per_person":{},"confidence":0.6}'
        stats = {"model": "h", "tokens_in": 50, "tokens_out": 10, "duration_ms": 100, "gpu_ms": 80}

        task_id = await db.create_cognitive_task(
            type="reflection", payload={"for_date": "2026-05-19"}, priority=10,
        )
        task = {"id": task_id, "type": "reflection", "payload": {"for_date": "2026-05-19"}}

        call_models = []

        async def fake_topic(model, topic, config, num_ctx=None, system=None, timeout_s=300):
            call_models.append(model)
            return (bad_json if len(call_models) == 1 else good_json, stats)

        with patch("worker._call_ollama_topic", side_effect=fake_topic):
            await ReflectionWorker(cfg).handle(task, container=_worker_container(db))

        self.assertEqual(call_models, [
            "hermes3:8b-llama3.1-q6_K", "hermes3:8b-llama3.1-q6_K",
        ])


# ── upsert_routine reinforce_bump wiring ──────────────────────────────────────

class TestUpsertRoutineReinforceBump(unittest.IsolatedAsyncioTestCase):
    """Review A: confidence_bump from RoutineReinforcement now reaches the
    DB layer as the +Δ used on conflict, instead of being silently ignored
    (the old hard-coded +0.1)."""

    async def asyncSetUp(self):
        import tempfile, database as db
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "bump.db")
        await db.init_db()

    async def asyncTearDown(self):
        import database as db
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_reinforce_bump_is_applied(self):
        import database as db
        # Insert with baseline 0.5.
        await db.upsert_routine("dad", "morning coffee", {}, confidence=0.5)
        # Reinforce with bump=0.25 → expect 0.75, not 0.6 (the old +0.1).
        await db.upsert_routine("dad", "morning coffee", {}, confidence=0.5, reinforce_bump=0.25)
        routines = await db.get_routines(person_id="dad")
        self.assertEqual(len(routines), 1)
        self.assertAlmostEqual(routines[0]["confidence"], 0.75, places=5)

    async def test_reinforce_bump_clamped_to_0_3(self):
        import database as db
        await db.upsert_routine("dad", "bedtime", {}, confidence=0.5)
        # An out-of-band bump from a misbehaving model gets clamped to 0.3,
        # not silently passed through. 0.5 + 0.3 = 0.8.
        await db.upsert_routine("dad", "bedtime", {}, confidence=0.5, reinforce_bump=0.9)
        routines = await db.get_routines(person_id="dad")
        self.assertAlmostEqual(routines[0]["confidence"], 0.8, places=5)

    async def test_reinforce_bump_capped_at_1(self):
        """Existing 1.0 confidence stays at 1.0 even with a positive bump."""
        import database as db
        await db.upsert_routine("dad", "sleep", {}, confidence=1.0)
        await db.upsert_routine("dad", "sleep", {}, confidence=0.5, reinforce_bump=0.3)
        routines = await db.get_routines(person_id="dad")
        self.assertAlmostEqual(routines[0]["confidence"], 1.0, places=5)


# ── Reflection per_person dedup ────────────────────────────────────────────────

class TestReflectionPerPersonDedup(unittest.IsolatedAsyncioTestCase):
    """Review B: the LLM may emit both 'Dad' and 'dad'. The worker
    must lowercase first and dedup before writing — Python dict iteration
    order is not a stable contract to rely on for last-write-wins."""

    async def asyncSetUp(self):
        import tempfile, database as db
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "dedup.db")
        await db.init_db()

    async def asyncTearDown(self):
        import database as db
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_mixed_case_keys_collapse_to_one_row(self):
        import database as db
        from cognitive_workers.reflection import ReflectionWorker

        cfg = {
            "cognitive_workers": {"reflection": {
                "default_model": "hermes3:8b-llama3.1-q6_K",
                "upgrade_model": None,
                "num_ctx": 8192, "max_runtime_s": 60,
            }},
        }
        # Mixed-case duplicate keys. Both contain "Dad" — only one row
        # should land. (last-write-wins after lowercase normalization).
        out = (
            '{"household_summary":"calm",'
            ' "per_person":{"Dad":"first","dad":"second"},'
            ' "confidence":0.7}'
        )
        stats = {"model": "h", "tokens_in": 50, "tokens_out": 10, "duration_ms": 100, "gpu_ms": 80}

        task_id = await db.create_cognitive_task(
            type="reflection", payload={"for_date": "2026-05-20"}, priority=10,
        )
        task = {"id": task_id, "type": "reflection", "payload": {"for_date": "2026-05-20"}}

        with patch("worker._call_ollama_topic",
                   new=AsyncMock(return_value=(out, stats))):
            result = await ReflectionWorker(cfg).handle(task, container=_worker_container(db))

        # household + 1 deduped dad row = 2 (not 3).
        self.assertEqual(result["_result"]["rows"], 2)
        dad = await db.get_tomorrow_context("2026-05-20", person_id="dad")
        self.assertIsNotNone(dad)
        # The exact winner is implementation-defined (dict insertion order),
        # but it must be one of the two summaries — never a hybrid or empty.
        self.assertIn(dad["summary"], ("first", "second"))


# ── Fail-loud on empty Ollama text ────────────────────────────────────────────

class TestEmptyOllamaTextRaises(unittest.IsolatedAsyncioTestCase):
    """Follow-up review Warning #1: empty text from Ollama (transport hiccup,
    dropped socket) must remain fail-loud after one retry. The previous
    pre-Phase-28.5 behaviour silently emitted empty output and looked like
    a healthy zero-row run; restoring fail-loud lets Watchman see the
    failed cognitive_task."""

    async def asyncSetUp(self):
        import tempfile, database as db
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "empty_fail.db")
        await db.init_db()

    async def asyncTearDown(self):
        import database as db
        if hasattr(db, "close_db"):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_reflection_raises_on_empty_then_empty(self):
        """Two empty responses in a row → the worker raises so the
        cognitive_task is marked failed."""
        import database as db
        from cognitive_workers.reflection import ReflectionWorker

        cfg = {
            "cognitive_workers": {"reflection": {
                "default_model": "hermes3:8b-llama3.1-q6_K",
                "upgrade_model": None,
                "num_ctx": 8192, "max_runtime_s": 60,
            }},
        }
        stats = {"model": "h", "tokens_in": 0, "tokens_out": 0, "duration_ms": 100, "gpu_ms": 50}

        task_id = await db.create_cognitive_task(
            type="reflection", payload={"for_date": "2026-05-22"}, priority=10,
        )
        task = {"id": task_id, "type": "reflection", "payload": {"for_date": "2026-05-22"}}

        # Both calls return empty — transport stayed dark across the retry.
        with patch("worker._call_ollama_topic",
                   new=AsyncMock(side_effect=[("", stats), ("", stats)])):
            with self.assertRaises(RuntimeError) as ctx:
                await ReflectionWorker(cfg).handle(task, container=_worker_container(db))

        self.assertIn("Ollama returned no text", str(ctx.exception))

    async def test_consolidation_raises_on_empty_then_empty(self):
        """Same fail-loud semantics for consolidation. The OLD silent-empty
        meant weeks of failed consolidation passes masquerading as healthy
        zero-write runs — keeping the regression guard explicit."""
        import database as db
        from cognitive_workers.consolidation import MemoryConsolidationWorker

        cfg = {
            "cognitive_workers": {"consolidation": {
                "default_model": "hermes3:8b-llama3.1-q6_K",
                "upgrade_model": None,
                "num_ctx": 8192, "max_runtime_s": 60,
            }},
        }
        stats = {"model": "h", "tokens_in": 0, "tokens_out": 0, "duration_ms": 100, "gpu_ms": 50}

        task_id = await db.create_cognitive_task(
            type="consolidation", payload={"person_id": "dad"}, priority=10,
        )
        task = {"id": task_id, "type": "consolidation", "payload": {"person_id": "dad"}}

        with patch("worker._call_ollama_topic",
                   new=AsyncMock(side_effect=[("", stats), ("", stats)])):
            with self.assertRaises(RuntimeError) as ctx:
                await MemoryConsolidationWorker(cfg).handle(task, container=_worker_container(db))

        self.assertIn("Ollama returned no text", str(ctx.exception))

    async def test_empty_then_good_recovers(self):
        """Empty first call followed by valid retry = clean recovery,
        no exception, full row write. Proves the empty-text retry path
        is wired and not a no-op."""
        import database as db
        from cognitive_workers.reflection import ReflectionWorker

        cfg = {
            "cognitive_workers": {"reflection": {
                "default_model": "hermes3:8b-llama3.1-q6_K",
                "upgrade_model": None,
                "num_ctx": 8192, "max_runtime_s": 60,
            }},
        }
        good = '{"household_summary":"clean run","per_person":{},"confidence":0.7}'
        stats_empty = {"model": "h", "tokens_in": 0, "tokens_out": 0, "duration_ms": 100, "gpu_ms": 50}
        stats_good = {"model": "h", "tokens_in": 80, "tokens_out": 20, "duration_ms": 250, "gpu_ms": 200}

        task_id = await db.create_cognitive_task(
            type="reflection", payload={"for_date": "2026-05-22"}, priority=10,
        )
        task = {"id": task_id, "type": "reflection", "payload": {"for_date": "2026-05-22"}}

        with patch("worker._call_ollama_topic",
                   new=AsyncMock(side_effect=[("", stats_empty), (good, stats_good)])):
            result = await ReflectionWorker(cfg).handle(task, container=_worker_container(db))

        self.assertEqual(result["_result"]["rows"], 1)
        # Both stats summed — keeps cost accounting honest under transport retry.
        self.assertEqual(result["_stats"]["tokens_in"], 80)
        self.assertEqual(result["_stats"]["tokens_out"], 20)

    async def test_research_query_empty_does_not_raise(self):
        """Research opts out of fail-loud via raise_on_empty=False because
        an empty queries response = "model thinks we have enough" is a
        legitimate iteration-ending signal, not a transport failure."""
        import database as db
        from cognitive_workers.research import ResearchWorker

        cfg = {
            "ollama_base_url": "http://x",
            "searxng_url": "http://x:8081",
            "cognitive_workers": {"research": {
                "default_model": "qwen2.5:14b", "upgrade_model": None,
                "escalate_above_tokens": 9999, "num_ctx": 8192,
                "max_runtime_s": 60, "max_iterations": 1, "max_urls_per_iteration": 1,
            }},
        }
        stats = {"model": "qwen", "tokens_in": 5, "tokens_out": 2, "duration_ms": 30, "gpu_ms": 20}
        call_log = []

        async def fake_ollama(model, prompt, config, num_ctx=None, system=None, timeout_s=120):
            if system and "research planner" in system:
                call_log.append("query")
                return ("", stats)  # both query attempts empty
            if system and "Synthesise" in system:
                call_log.append("synth")
                return ("# Synth\nNothing.", stats)
            return ("noop", stats)

        task_id = await db.create_cognitive_task(
            type="research", payload={"topic": "x", "requester_id": "dad"},
            actor_id="123", channel_id="456",
        )
        task = {"id": task_id, "type": "research", "actor_id": "123", "channel_id": "456",
                "payload": {"topic": "x", "requester_id": "dad"}}

        with patch("worker._call_ollama_topic", side_effect=fake_ollama):
            # Must NOT raise — research swallows empty queries and stops the loop.
            await ResearchWorker(cfg).handle(task, container=_worker_container(db))

        # Initial query + retry on empty = 2 query calls, then iteration
        # ends naturally, synth runs once.
        self.assertEqual(call_log.count("query"), 2)
        self.assertEqual(call_log.count("synth"), 1)


# ── Digest fallback routing ───────────────────────────────────────────────────

class TestDigestFallbackUsesOllamaTopic(unittest.TestCase):
    """Follow-up review test gap: prove _ollama_direct_for_digest routes
    through worker._call_ollama_topic, NOT through claude_service._call_ollama.
    Routing through the latter prepends today's weather/presence as "LIVE
    CONTEXT" — wrong for a historical-analysis prompt about yesterday."""

    def test_helper_invokes_call_ollama_topic_with_clean_system(self):
        import nightly_digest

        captured = {}

        async def fake_topic(model, topic, config, num_ctx=None, system=None, timeout_s=300):
            captured["model"] = model
            captured["topic"] = topic
            captured["system"] = system
            return "fallback-output", {
                "model": model, "tokens_in": 0, "tokens_out": 0,
                "duration_ms": 0, "gpu_ms": 0,
            }

        # If anyone ever re-routes through claude_service._call_ollama by
        # accident, this mock surfaces it as a noisy AttributeError instead
        # of a silent regression.
        with patch("worker._call_ollama_topic", side_effect=fake_topic), \
             patch("claude_service._call_ollama",
                   side_effect=AssertionError("must not route via claude_service._call_ollama")):
            text = asyncio.run(nightly_digest._ollama_direct_for_digest(
                system="You extract insights.",
                messages=[{"role": "user", "content": "yesterday's chat"}],
                config={"ollama_base_url": "http://x"},
                model="hermes3:8b-llama3.1-q6_K",
            ))

        self.assertEqual(text, "fallback-output")
        self.assertEqual(captured["model"], "hermes3:8b-llama3.1-q6_K")
        self.assertEqual(captured["system"], "You extract insights.",
                         "system prompt must be passed through unmodified — "
                         "no LIVE CONTEXT prepend")
        self.assertEqual(captured["topic"], "yesterday's chat",
                         "user message must flatten to topic without "
                         "additional live-context content")


# ── Test-log path sanitization ─────────────────────────────────────────────────

class TestBernieTestLogSanitization(unittest.TestCase):
    """Review H: BERNIE_TEST_LOG must refuse paths inside /data/. The
    bootstrap exists specifically to prevent test ERROR/WARNING lines from
    polluting the production log Watchman audits.

    Tests call the validator function directly — re-importing the tests
    package would create new logging FileHandlers and prevent Python
    from exiting cleanly within the pre-commit hook's 30s window."""

    def test_data_path_raises_runtime_error(self):
        from tests import _validate_test_log_path
        with self.assertRaises(RuntimeError) as ctx:
            _validate_test_log_path("/data/bernie_test.log")
        self.assertIn("/data/", str(ctx.exception))

    def test_data_subpath_also_rejected(self):
        from tests import _validate_test_log_path
        with self.assertRaises(RuntimeError):
            _validate_test_log_path("/data/logs/test.log")

    def test_path_starting_with_data_rejected(self):
        from tests import _validate_test_log_path
        with self.assertRaises(RuntimeError):
            _validate_test_log_path("/data")

    def test_tmp_path_is_accepted(self):
        from tests import _validate_test_log_path
        _validate_test_log_path("/tmp/bernie_test.log")  # must not raise

    def test_user_home_path_is_accepted(self):
        from tests import _validate_test_log_path
        _validate_test_log_path("/home/red/bernie_test.log")  # must not raise


if __name__ == "__main__":
    unittest.main()
