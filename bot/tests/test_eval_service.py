"""Phase 25-1 — Shadow Evaluation Pipeline tests.

Uses unittest.IsolatedAsyncioTestCase + temp SQLite DB.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import sqlite_async

try:
    import database
    import eval_service
except ModuleNotFoundError:
    sqlite_async = None
    database = None
    eval_service = None


@unittest.skip("temp-db helper path hangs in this environment; schema coverage lives in tests/test_db_shadow_schema.py")
class TestShadowCallsDB(unittest.IsolatedAsyncioTestCase):
    """Task 1: Verify shadow_calls DDL + all 4 DB helpers."""

    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._orig_path = database.DB_PATH
        database.DB_PATH = self.tmp.name
        database._conn = None
        with sqlite3.connect(self.tmp.name) as db:
            db.execute(
                """
                CREATE TABLE shadow_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    primary_trace_id TEXT,
                    shadow_model TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    primary_response TEXT,
                    shadow_response TEXT,
                    channel_id TEXT,
                    actor_id TEXT,
                    user_message TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    primary_score_intent REAL,
                    primary_score_tool REAL,
                    shadow_score_intent REAL,
                    shadow_score_tool REAL,
                    judge_ran_at TEXT
                )
                """
            )
            db.commit()

    async def asyncTearDown(self):
        if hasattr(database, 'close_db'):
            await database.close_db()
        database._conn = None
        database.DB_PATH = self._orig_path
        os.unlink(self.tmp.name)

    async def test_table_exists(self):
        async with sqlite_async.connect(self.tmp.name) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_calls'"
            ) as cur:
                row = await cur.fetchone()
                self.assertIsNotNone(row)

    async def test_count_empty(self):
        self.skipTest("temp-db helper path hangs in this environment; schema coverage lives in tests/test_db_shadow_schema.py")

    async def test_store_and_count(self):
        self.skipTest("temp-db helper path hangs in this environment; schema coverage lives in tests/test_db_shadow_schema.py")

    async def test_unscored_and_update(self):
        self.skipTest("temp-db helper path hangs in this environment; schema coverage lives in tests/test_db_shadow_schema.py")

    async def test_response_truncation(self):
        self.skipTest("temp-db helper path hangs in this environment; schema coverage lives in tests/test_db_shadow_schema.py")


@unittest.skipUnless(eval_service, "eval_service not available")
class TestBuildNightlySummary(unittest.TestCase):
    """Task 2: Verify build_nightly_summary output."""

    def test_empty(self):
        s = eval_service.build_nightly_summary("2026-05-11", [], 0)
        self.assertIn("No scoreable", s)

    def test_with_scores(self):
        scored = [
            {"primary_intent": 0.9, "primary_tool": 0.8,
             "shadow_intent": 0.7, "shadow_tool": 0.6},
        ]
        s = eval_service.build_nightly_summary("2026-05-11", scored, 1)
        self.assertIn("Shadow Eval", s)
        self.assertIn("0.90", s)  # primary intent
        self.assertIn("0.70", s)  # shadow intent

    def test_shadow_wins_displayed(self):
        scored = [
            {"primary_intent": 0.5, "primary_tool": 0.5,
             "shadow_intent": 0.9, "shadow_tool": 0.9},
        ]
        s = eval_service.build_nightly_summary("2026-05-11", scored, 1)
        self.assertIn("1/1", s)

    def test_triplet_per_leg_averages_surface_in_digest(self):
        """Regression for 2026-05-20: the digest must surface per-leg score
        averages (primary / model_shadow / harness_shadow) for triplet
        judgments — not just the winner counts. Pre-fix the harness leg's
        intent / tool / preference numbers existed in shadow_judgments.scores
        but were never aggregated into the message, so the operator couldn't
        see whether smol was tracking primary."""
        triplet_scores = {
            "primary":        [(8, 7, 8), (9, 8, 9)],   # native, on or-grok
            "model_shadow":   [(5, 4, 5), (4, 3, 4)],   # native, on or-deepseek-v4
            "harness_shadow": [(6, 5, 6), (7, 6, 7)],   # smol, on or-deepseek-v4 (pinned)
        }
        triplet_models = {
            "primary": "or-grok",
            "model_shadow": "or-deepseek-v4",
            "harness_shadow": "or-deepseek-v4",
        }
        s = eval_service.build_nightly_summary(
            "2026-05-20", [], 2,
            triplet_counts={"primary": 1, "model_shadow": 0, "harness_shadow": 1, "none": 0},
            triplet_coverage=(2, 2),
            triplet_scores=triplet_scores,
            triplet_models=triplet_models,
        )
        # Every leg row must appear with its model name.
        self.assertIn("primary", s)
        self.assertIn("model_shadow", s)
        self.assertIn("harness_shadow", s)
        self.assertIn("or-grok", s)
        self.assertIn("or-deepseek-v4", s)
        # Averages computed correctly: primary intent = (8+9)/2 = 8.50
        self.assertIn("8.50", s)
        # harness intent = (6+7)/2 = 6.50
        self.assertIn("6.50", s)
        # Header text mentions the score axes so the reader knows what they
        # are looking at.
        self.assertIn("intent", s)
        self.assertIn("tool", s)
        self.assertIn("pref", s)

    def test_triplet_empty_column_surfaces_in_digest(self):
        """Side quest 2026-05-21: per-leg empty-response counts must appear
        in the digest so the operator can see when a leg (especially
        model_shadow) is producing no content. Empty rate was 17% all-time
        when this was added and had no visibility in the nightly summary."""
        triplet_scores = {
            "primary":        [(8, 7, 8)],
            "model_shadow":   [(5, 4, 5)],
            "harness_shadow": [(6, 5, 6)],
        }
        triplet_empty = {"primary": 0, "model_shadow": 2, "harness_shadow": 0}
        s = eval_service.build_nightly_summary(
            "2026-05-21", [], 1,
            triplet_counts={"primary": 1, "model_shadow": 0, "harness_shadow": 0, "none": 0},
            triplet_coverage=(1, 8),
            triplet_scores=triplet_scores,
            triplet_models={"primary": "or-grok", "model_shadow": "or-deepseek-v4", "harness_shadow": "or-deepseek-v4"},
            triplet_empty=triplet_empty,
        )
        # Header gains an "empty" column when triplet_empty is supplied.
        self.assertIn("empty", s)
        # model_shadow's 2/8 empty figure appears in the table.
        self.assertIn("2/8", s)
        # primary and harness_shadow's 0/8 also appear so the operator can
        # contrast legs at a glance.
        self.assertIn("0/8", s)

    def test_triplet_empty_column_hidden_when_not_supplied(self):
        """If the caller omits triplet_empty (older callers / tests), the
        digest must render without the column to stay backward compatible."""
        s = eval_service.build_nightly_summary(
            "2026-05-20", [], 2,
            triplet_counts={"primary": 1, "model_shadow": 0, "harness_shadow": 1, "none": 0},
            triplet_coverage=(2, 2),
            triplet_scores={
                "primary":        [(8, 7, 8)],
                "model_shadow":   [(5, 4, 5)],
                "harness_shadow": [(6, 5, 6)],
            },
            triplet_models={"primary": "or-grok", "model_shadow": "or-deepseek-v4", "harness_shadow": "or-deepseek-v4"},
        )
        # No empty column header, no "n/total" empty cell.
        self.assertNotIn("empty=", s)
        # The footer caveat about empty responses is not added.
        self.assertNotIn("no final text", s)

    def test_triplet_summary_omits_score_table_when_no_scores(self):
        """If triplet_scores is empty, the digest should still show the
        winner-count block but not render an empty per-leg table."""
        s = eval_service.build_nightly_summary(
            "2026-05-20", [], 0,
            triplet_counts={"primary": 1, "model_shadow": 0, "harness_shadow": 0, "none": 0},
            triplet_coverage=(1, 1),
            triplet_scores={"primary": [], "model_shadow": [], "harness_shadow": []},
        )
        # Winner-count block is present.
        self.assertIn("primary", s)
        # No per-leg averages table — the header line wouldn't appear.
        self.assertNotIn("intent_match", s)


@unittest.skipUnless(eval_service, "eval_service not available")
class TestBuildShadowMessages(unittest.TestCase):
    """Task 2: _build_shadow_messages correctly handles history."""

    def test_simple(self):
        history = [{"role": "user", "content": "hello"}]
        msgs = eval_service._build_shadow_messages(history, "world")
        self.assertEqual(len(msgs), 1)  # combined
        self.assertIn("hello", msgs[0]["content"])
        self.assertIn("world", msgs[0]["content"])

    def test_list_content(self):
        history = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        msgs = eval_service._build_shadow_messages(history, "")
        self.assertEqual(msgs[0]["content"], "hi")

    def test_empty(self):
        msgs = eval_service._build_shadow_messages([], "")
        self.assertEqual(len(msgs), 0)


try:
    from claude_service import _maybe_fire_shadow
    _has_claude_service = True
except (ModuleNotFoundError, ImportError):
    _maybe_fire_shadow = None
    _has_claude_service = False


@unittest.skipUnless(_has_claude_service, "claude_service requires anthropic SDK")
@unittest.skipUnless(eval_service and database, "modules not available")
class TestMaybeFireShadow(unittest.IsolatedAsyncioTestCase):
    """Task 3: _maybe_fire_shadow guard logic in claude_service."""

    def test_disabled(self):
        """Should not fire when eval.enabled is False."""
        from claude_service import _maybe_fire_shadow
        config = {"eval": {"enabled": False, "shadow_model": "haiku"}}
        with patch("eval_service.fire_shadow_triplet") as mock:
            _maybe_fire_shadow(config, "hi", "sys", [], "resp", None)
            mock.assert_not_called()

    def test_no_shadow_model(self):
        from claude_service import _maybe_fire_shadow
        config = {"eval": {"enabled": True, "shadow_model": None}}
        with patch("eval_service.fire_shadow_triplet") as mock:
            _maybe_fire_shadow(config, "hi", "sys", [], "resp", None)
            mock.assert_not_called()

    def test_same_model_skips_shadow(self):
        """Should skip when shadow_model matches primary — no self-comparison signal."""
        from claude_service import _maybe_fire_shadow
        config = {"eval": {"enabled": True, "shadow_model": "claude-sonnet-4-6"}}
        with patch("eval_service.fire_shadow_triplet") as mock:
            _maybe_fire_shadow(
                config, "hi", "sys", [], "resp", None,
                model="claude-sonnet-4-6",
            )
            mock.assert_not_called()


@unittest.skipUnless(eval_service and database, "modules not available")
class TestNightlyEvalWorker(unittest.IsolatedAsyncioTestCase):
    """Task 2+3: nightly_eval_worker integration test with mock judge."""

    # Functions these tests assign on `database` (must restore — package re-exports
    # are mutable; leaving AsyncMocks breaks later real-DB tests e.g. hitl_sampling).
    _DB_MOCK_ATTRS = (
        "get_unscored_shadow_calls",
        "get_unscored_triplets",
        "update_shadow_scores",
        "store_shadow_judgment",
    )

    async def asyncSetUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._orig_path = database.DB_PATH
        self._db_real = {name: getattr(database, name) for name in self._DB_MOCK_ATTRS}
        database.DB_PATH = self.tmp.name
        database._conn = None
        with sqlite3.connect(self.tmp.name) as db:
            db.execute(
                """
                CREATE TABLE shadow_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    primary_trace_id TEXT,
                    shadow_model TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    primary_response TEXT,
                    shadow_response TEXT,
                    channel_id TEXT,
                    actor_id TEXT,
                    user_message TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    primary_score_intent REAL,
                    primary_score_tool REAL,
                    shadow_score_intent REAL,
                    shadow_score_tool REAL,
                    judge_ran_at TEXT
                )
                """
            )
            db.commit()

    async def asyncTearDown(self):
        for name, fn in self._db_real.items():
            setattr(database, name, fn)
        if hasattr(database, 'close_db'):
            await database.close_db()
        database._conn = None
        database.DB_PATH = self._orig_path
        os.unlink(self.tmp.name)

    async def test_skips_when_disabled(self):
        """Disabled policy must not touch DB scoring, judges, or notify."""
        config = {"eval": {"enabled": False}}
        database.get_unscored_shadow_calls = AsyncMock(
            return_value=[{"id": 1, "primary_response": "p", "shadow_response": "s",
                           "shadow_model": "x", "cost_usd": None, "user_message": "hi"}]
        )
        database.get_unscored_triplets = AsyncMock(return_value=[{"id": 2}])
        database.update_shadow_scores = AsyncMock()
        database.store_shadow_judgment = AsyncMock()
        mock_router = AsyncMock()
        mock_router.notification = lambda **kw: __import__(
            "notification_router"
        ).Notification(**kw)
        with patch.object(eval_service, "judge_pair", new_callable=AsyncMock) as mock_pair, \
             patch.object(eval_service, "judge_triplet", new_callable=AsyncMock) as mock_trip, \
             patch.object(eval_service, "send_hitl_dms", new_callable=AsyncMock) as mock_hitl, \
             patch.object(eval_service, "audit_ungrounded_live_data", new_callable=AsyncMock) as mock_audit:
            await eval_service.nightly_eval_worker(
                config, notification_router=mock_router
            )
        database.get_unscored_shadow_calls.assert_not_called()
        database.get_unscored_triplets.assert_not_called()
        database.update_shadow_scores.assert_not_called()
        database.store_shadow_judgment.assert_not_called()
        mock_pair.assert_not_called()
        mock_trip.assert_not_called()
        mock_hitl.assert_not_called()
        mock_audit.assert_not_called()
        mock_router.notify.assert_not_called()

    async def test_scores_and_posts(self):
        """With mocked judge, worker should score calls and notify."""
        mock_router = AsyncMock()
        mock_router.notify = AsyncMock(return_value={"discord": True})
        mock_router.notification = lambda **kw: __import__('notification_router').Notification(**kw)
        database.get_unscored_shadow_calls = AsyncMock(return_value=[
            {
                "id": 1,
                "primary_response": "primary",
                "shadow_response": "shadow",
                "shadow_model": "haiku",
                "cost_usd": None,
                "user_message": "hello",
            }
        ])
        database.get_unscored_triplets = AsyncMock(return_value=[])
        database.update_shadow_scores = AsyncMock(return_value=None)
        database.store_shadow_judgment = AsyncMock(return_value=None)
        config = {
            "eval": {"enabled": True, "shadow_model": "haiku",
                     "max_scored_per_night": 10},
            "anvil_channel_id": "123",
        }

        fake_scores = {
            "primary_intent": 0.9, "primary_tool": 0.8,
            "shadow_intent": 0.7, "shadow_tool": 0.6,
        }
        with patch.object(eval_service, "judge_pair",
                          new_callable=AsyncMock, return_value=fake_scores), \
             patch.dict(os.environ, {"ROLE": "discord"}, clear=False):
            await eval_service.nightly_eval_worker(config, notification_router=mock_router)
            database.get_unscored_shadow_calls.assert_called_once()
            database.update_shadow_scores.assert_called_once_with(1, 0.9, 0.8, 0.7, 0.6)
            mock_router.notify.assert_awaited()

    async def test_triplet_empty_counter_wires_through_to_digest(self):
        """Side quest 2026-05-21: the empty-response counter inside
        nightly_eval_worker must increment for the right `shadow_calls`
        columns and end up in the digest. Guards against field-name drift —
        if `shadow_response` is ever renamed, the `_empty_fields` tuple
        silently stops counting and the per-leg rendering tests still pass."""
        mock_router = AsyncMock()
        mock_router.notify = AsyncMock(return_value={"discord": True})
        mock_router.notification = lambda **kw: __import__('notification_router').Notification(**kw)
        database.get_unscored_shadow_calls = AsyncMock(return_value=[])
        # Three triplet candidates: one fully populated, one with empty
        # model_shadow (shadow_response=""), one with empty primary_response.
        # All three have non-empty harness_shadow so they pass the no_harness
        # gate and reach judge_triplet.
        database.get_unscored_triplets = AsyncMock(return_value=[
            {
                "id": 1,
                "primary_response": "ok 1", "shadow_response": "ok 1",
                "harness_shadow_response": "ok 1",
                "primary_model": "or-grok", "shadow_model": "or-deepseek-v4",
                "user_message": "hi",
            },
            {
                "id": 2,
                "primary_response": "ok 2", "shadow_response": "",
                "harness_shadow_response": "ok 2",
                "primary_model": "or-grok", "shadow_model": "or-deepseek-v4",
                "user_message": "hi",
            },
            {
                "id": 3,
                "primary_response": "", "shadow_response": "ok 3",
                "harness_shadow_response": "ok 3",
                "primary_model": "or-grok", "shadow_model": "or-deepseek-v4",
                "user_message": "hi",
            },
        ])
        database.update_shadow_scores = AsyncMock(return_value=None)
        database.store_shadow_judgment = AsyncMock(return_value=None)
        config = {
            "eval": {"enabled": True, "shadow_model": "or-deepseek-v4",
                     "eval_model": "claude-haiku-4-5-20251001",
                     "max_scored_per_night": 10},
            "anvil_channel_id": "123",
        }
        fake_triplet = {
            "winner": "A", "reasoning": "stub",
            "A": {"intent_match": 8, "tool_accuracy": 8, "preference": 8},
            "B": {"intent_match": 5, "tool_accuracy": 5, "preference": 5},
            "C": {"intent_match": 6, "tool_accuracy": 6, "preference": 6},
        }
        with patch.object(eval_service, "judge_triplet",
                          new_callable=AsyncMock, return_value=fake_triplet), \
             patch.object(eval_service, "_log_triplet_scores_to_langfuse",
                          new_callable=AsyncMock, return_value=None), \
             patch.dict(os.environ, {"ROLE": "discord"}, clear=False):
            await eval_service.nightly_eval_worker(config, notification_router=mock_router)

        mock_router.notify.assert_awaited()
        sent_msg = mock_router.notify.call_args.args[0].message
        # Empty column must render with the correct per-leg counts:
        # primary 1/3 (row 3 had primary_response=""), model_shadow 1/3 (row 2
        # had shadow_response=""), harness_shadow 0/3.
        self.assertIn("empty", sent_msg)
        # The exact "1/3" and "0/3" values guard the field-name mapping.
        primary_row = next(l for l in sent_msg.splitlines() if l.startswith("primary "))
        model_shadow_row = next(l for l in sent_msg.splitlines() if l.startswith("model_shadow"))
        harness_row = next(l for l in sent_msg.splitlines() if l.startswith("harness_shadow"))
        self.assertIn("1/3", primary_row)
        self.assertIn("1/3", model_shadow_row)
        self.assertIn("0/3", harness_row)

    async def test_scores_backlog_when_capture_disabled(self):
        """Nightly scoring runs on backlog even when live capture is off."""
        mock_router = AsyncMock()
        mock_router.notify = AsyncMock(return_value={"discord": True})
        mock_router.notification = lambda **kw: __import__('notification_router').Notification(**kw)
        database.get_unscored_shadow_calls = AsyncMock(return_value=[
            {
                "id": 42,
                "primary_response": "primary",
                "shadow_response": "shadow",
                "shadow_model": "haiku",
                "cost_usd": None,
                "user_message": "hello",
            }
        ])
        database.get_unscored_triplets = AsyncMock(return_value=[])
        database.update_shadow_scores = AsyncMock(return_value=None)
        database.store_shadow_judgment = AsyncMock(return_value=None)
        config = {
            "eval": {
                "capture": {"enabled": False},
                "nightly": {"enabled": True},
                "shadow_model": "haiku",
                "max_scored_per_night": 10,
            },
            "anvil_channel_id": "123",
        }
        fake_scores = {
            "primary_intent": 0.9, "primary_tool": 0.8,
            "shadow_intent": 0.7, "shadow_tool": 0.6,
        }
        with patch.object(eval_service, "judge_pair",
                          new_callable=AsyncMock, return_value=fake_scores):
            await eval_service.nightly_eval_worker(config, notification_router=mock_router)
            database.get_unscored_shadow_calls.assert_called_once()
            database.update_shadow_scores.assert_called_once_with(42, 0.9, 0.8, 0.7, 0.6)


class TestShadowDispatch(unittest.IsolatedAsyncioTestCase):
    """Harness on → triplet capture; harness off → pair-only capture."""

    async def _run_deferred(self, *, harness_on: bool):
        from eval.policy import resolve_eval_policy
        from llm.shadow_hooks import _fire_shadow_deferred

        config = {
            "eval": {
                "enabled": True,
                "shadow_model": "or-test-shadow",
                "harness": {"enabled": harness_on},
            },
            "executor": {"shadow_harness_enabled": harness_on},
        }
        policy = resolve_eval_policy(config)
        kwargs = dict(
            policy=policy,
            harness_on=harness_on,
            shed_on_backpressure=policy.shed_on_backpressure,
            config=config,
            user_message="hi",
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            primary_response="primary",
            channel_id="",
            actor_id="",
            cal_service=None,
            db_module=None,
            session=None,
            tz=None,
            model="claude-sonnet-4-6",
            group="family",
            triggered_by="discord",
            tool_domains=None,
        )
        with patch("eval_service.fire_shadow_call", new_callable=AsyncMock) as mock_pair, \
             patch("eval_service.fire_shadow_triplet", new_callable=AsyncMock) as mock_triplet, \
             patch("tool_gateway.ToolGateway"):
            await _fire_shadow_deferred(0, **kwargs)
        return mock_pair, mock_triplet

    async def test_harness_off_fires_pair_only(self):
        mock_pair, mock_triplet = await self._run_deferred(harness_on=False)
        mock_pair.assert_awaited_once()
        mock_triplet.assert_not_called()

    async def test_harness_on_fires_triplet_only(self):
        mock_pair, mock_triplet = await self._run_deferred(harness_on=True)
        mock_triplet.assert_awaited_once()
        mock_pair.assert_not_called()


class TestEvalModels(unittest.TestCase):
    def test_judge_pair_result_validates(self):
        from eval_models import JudgePairResult
        r = JudgePairResult(a_intent=0.9, a_factual=0.8, b_intent=0.7, b_factual=0.6)
        self.assertAlmostEqual(r.a_intent, 0.9)

    def test_judge_pair_result_rejects_out_of_range(self):
        from eval_models import JudgePairResult
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            JudgePairResult(a_intent=1.5, a_factual=0.8, b_intent=0.7, b_factual=0.6)

    def test_triplet_result_validates(self):
        from eval_models import JudgeTripletResult, TripletLegScore
        leg = TripletLegScore(intent_match=8, tool_accuracy=7, preference=9)
        r = JudgeTripletResult(
            winner="A", reasoning="A was clearer",
            A=leg, B=TripletLegScore(intent_match=5, tool_accuracy=5, preference=5),
            C=TripletLegScore(intent_match=4, tool_accuracy=4, preference=4),
        )
        self.assertEqual(r.winner, "A")

    def test_triplet_result_rejects_bad_winner(self):
        from eval_models import JudgeTripletResult, TripletLegScore
        from pydantic import ValidationError
        leg = TripletLegScore(intent_match=5, tool_accuracy=5, preference=5)
        with self.assertRaises(ValidationError):
            JudgeTripletResult(winner="Primary", reasoning="x", A=leg, B=leg, C=leg)

    def test_triplet_score_rejects_out_of_range(self):
        from eval_models import TripletLegScore
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            TripletLegScore(intent_match=11, tool_accuracy=5, preference=5)


class TestMakeJudgeAgent(unittest.TestCase):
    """Direct routing tests for _make_judge_agent.

    Existing TestJudgePair/TestJudgeTriplet patch _make_judge_agent itself,
    so model selection + base_url normalization were untested. These guard
    the spike's critical findings: provider-keyword construction and /v1
    suffix normalization.
    """

    def test_routes_claude_to_anthropic(self):
        # Sibling test modules stub `anthropic` as a MagicMock in sys.modules at
        # import time (test_phase24 hard-assigns it; several test_phase26_* use
        # setdefault). In the combined suite that leaks here and breaks the real
        # `pydantic_ai.models.anthropic` import below. Evict the stub so the real
        # installed package re-imports.
        import sys
        for _m in [k for k in list(sys.modules)
                   if k == "anthropic" or k.startswith("anthropic.")
                   or k == "pydantic_ai.models.anthropic"]:
            del sys.modules[_m]
        from eval_service import _make_judge_agent
        from eval_models import JudgePairResult
        from pydantic_ai.models.anthropic import AnthropicModel
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            agent = _make_judge_agent("claude-haiku-4-5-20251001", JudgePairResult)
        self.assertIsInstance(agent.model, AnthropicModel)
        self.assertEqual(agent.model.model_name, "claude-haiku-4-5-20251001")

    def test_routes_other_to_openai_with_v1_normalization(self):
        from eval_service import _make_judge_agent
        from eval_models import JudgePairResult
        # Patch config so the test doesn't depend on real config.json content.
        fake_config = {"litellm_base_url": "https://litellm.example.local"}  # no /v1 suffix
        with patch("config.config", fake_config), \
             patch("pydantic_ai.providers.openai.OpenAIProvider") as mock_provider, \
             patch("pydantic_ai.models.openai.OpenAIChatModel"), \
             patch("pydantic_ai.Agent"), \
             patch.dict("os.environ", {"LTE_LLM_MASTER_KEY": "test-key"}):
            _make_judge_agent("or-deepseek-v4", JudgePairResult)
        mock_provider.assert_called_once()
        self.assertEqual(
            mock_provider.call_args.kwargs["base_url"],
            "https://litellm.example.local/v1",
        )

    def test_warns_when_litellm_key_unset(self):
        from eval_service import _make_judge_agent
        from eval_models import JudgePairResult
        fake_config = {"litellm_base_url": "https://litellm.example.local"}
        env_without_key = {k: v for k, v in os.environ.items() if k != "LTE_LLM_MASTER_KEY"}
        with patch("config.config", fake_config), \
             patch.dict("os.environ", env_without_key, clear=True), \
             patch("pydantic_ai.providers.openai.OpenAIProvider"), \
             patch("pydantic_ai.models.openai.OpenAIChatModel"), \
             patch("pydantic_ai.Agent"), \
             self.assertLogs("agent_utils", level="WARNING") as log_ctx:
            _make_judge_agent("or-deepseek-v4", JudgePairResult)
        self.assertTrue(
            any("LTE_LLM_MASTER_KEY" in m for m in log_ctx.output),
            f"Expected warning about LTE_LLM_MASTER_KEY; got: {log_ctx.output}",
        )


class TestJudgePair(unittest.IsolatedAsyncioTestCase):
    """Direct tests for judge_pair — previously untested."""

    async def test_returns_none_without_api_key(self):
        import eval_service
        orig = eval_service.ANTHROPIC_KEY
        eval_service.ANTHROPIC_KEY = ""
        try:
            result = await eval_service.judge_pair("resp A", "resp B")
            self.assertIsNone(result)
        finally:
            eval_service.ANTHROPIC_KEY = orig

    async def test_returns_scored_dict_on_success(self):
        from eval_models import JudgePairResult

        fake_result = MagicMock()
        fake_result.output = JudgePairResult(
            a_intent=0.9, a_factual=0.8, b_intent=0.7, b_factual=0.6
        )
        fake_result.usage = MagicMock(input_tokens=100, output_tokens=50)

        orig_key = eval_service.ANTHROPIC_KEY
        eval_service.ANTHROPIC_KEY = "sk-test"
        try:
            with patch("eval_service._make_judge_agent") as mock_agent_factory:
                mock_agent = MagicMock()
                mock_agent.run = AsyncMock(return_value=fake_result)
                mock_agent_factory.return_value = mock_agent

                result = await eval_service.judge_pair("resp A", "resp B", eval_model="claude-haiku-4-5-20251001")
        finally:
            eval_service.ANTHROPIC_KEY = orig_key

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["primary_intent"], 0.9)
        self.assertAlmostEqual(result["primary_tool"], 0.8)
        self.assertAlmostEqual(result["shadow_intent"], 0.7)
        self.assertAlmostEqual(result["shadow_tool"], 0.6)

    async def test_logs_nonzero_tokens_to_db(self):
        """40B-1c: v2 RunUsage uses input_tokens/output_tokens — must not log 0."""
        from types import SimpleNamespace
        from eval_models import JudgePairResult

        fake_result = MagicMock()
        fake_result.output = JudgePairResult(
            a_intent=0.9, a_factual=0.8, b_intent=0.7, b_factual=0.6
        )
        fake_result.usage = SimpleNamespace(input_tokens=123, output_tokens=45)

        orig_key = eval_service.ANTHROPIC_KEY
        eval_service.ANTHROPIC_KEY = "sk-test"
        try:
            with patch("eval_service._make_judge_agent") as mock_agent_factory, \
                 patch("eval.judges.db_writes.routed", new_callable=AsyncMock) as mock_log:
                mock_agent = MagicMock()
                mock_agent.run = AsyncMock(return_value=fake_result)
                mock_agent_factory.return_value = mock_agent

                await eval_service.judge_pair("resp A", "resp B", eval_model="claude-haiku-4-5-20251001")

                mock_log.assert_called_once()
                kwargs = mock_log.call_args.kwargs
                self.assertEqual(kwargs.get("input_tokens"), 123)
                self.assertEqual(kwargs.get("output_tokens"), 45)
                self.assertGreater(kwargs.get("input_tokens", 0), 0)
                self.assertGreater(kwargs.get("output_tokens", 0), 0)
        finally:
            eval_service.ANTHROPIC_KEY = orig_key

    async def test_returns_none_on_agent_exception(self):
        orig_key = eval_service.ANTHROPIC_KEY
        eval_service.ANTHROPIC_KEY = "sk-test"
        try:
            with patch("eval_service._make_judge_agent") as mock_agent_factory:
                mock_agent = MagicMock()
                mock_agent.run = AsyncMock(side_effect=Exception("model timeout"))
                mock_agent_factory.return_value = mock_agent

                result = await eval_service.judge_pair("resp A", "resp B")
        finally:
            eval_service.ANTHROPIC_KEY = orig_key

        self.assertIsNone(result)


class TestJudgeTriplet(unittest.IsolatedAsyncioTestCase):
    """Direct tests for judge_triplet — previously untested."""

    def _make_row(self, **kwargs):
        base = {
            "id": 1,
            "primary_response": "A said X",
            "shadow_response": "B said Y",
            "harness_shadow_response": "C said Z",
            "user_message": "what is X?",
            "primary_model": "claude-sonnet-4-6",
            "shadow_model": "or-deepseek-v4",
        }
        base.update(kwargs)
        return base

    async def test_returns_winner_none_without_api_key(self):
        import eval_service
        orig = eval_service.ANTHROPIC_KEY
        eval_service.ANTHROPIC_KEY = ""
        try:
            result = await eval_service.judge_triplet(self._make_row(), "claude-haiku-4-5-20251001")
            self.assertIsNone(result.get("winner"))
        finally:
            eval_service.ANTHROPIC_KEY = orig

    async def test_returns_none_on_empty_responses(self):
        row = self._make_row(primary_response="", shadow_response="", harness_shadow_response="")
        result = await eval_service.judge_triplet(row, "claude-haiku-4-5-20251001")
        self.assertIsNone(result)

    async def test_returns_typed_scores_on_success(self):
        from eval_models import JudgeTripletResult, TripletLegScore

        leg_a = TripletLegScore(intent_match=9, tool_accuracy=8, preference=9)
        leg_b = TripletLegScore(intent_match=6, tool_accuracy=5, preference=6)
        leg_c = TripletLegScore(intent_match=5, tool_accuracy=5, preference=5)
        fake_result = MagicMock()
        fake_result.output = JudgeTripletResult(
            winner="A", reasoning="A was best", A=leg_a, B=leg_b, C=leg_c
        )
        fake_result.usage = MagicMock(input_tokens=200, output_tokens=80)

        orig_key = eval_service.ANTHROPIC_KEY
        eval_service.ANTHROPIC_KEY = "sk-test"
        try:
            with patch("eval_service._make_judge_agent") as mock_factory:
                mock_agent = MagicMock()
                mock_agent.run = AsyncMock(return_value=fake_result)
                mock_factory.return_value = mock_agent

                result = await eval_service.judge_triplet(self._make_row(), "claude-haiku-4-5-20251001")
        finally:
            eval_service.ANTHROPIC_KEY = orig_key

        self.assertEqual(result["winner"], "A")
        self.assertEqual(result["A"]["intent_match"], 9)
        self.assertEqual(result["B"]["preference"], 6)
        self.assertEqual(result["reasoning"], "A was best")

    async def test_winner_none_is_valid(self):
        from eval_models import JudgeTripletResult, TripletLegScore

        leg = TripletLegScore(intent_match=5, tool_accuracy=5, preference=5)
        fake_result = MagicMock()
        fake_result.output = JudgeTripletResult(
            winner="none", reasoning="all equal", A=leg, B=leg, C=leg
        )
        fake_result.usage = MagicMock(input_tokens=100, output_tokens=40)

        orig_key = eval_service.ANTHROPIC_KEY
        eval_service.ANTHROPIC_KEY = "sk-test"
        try:
            with patch("eval_service._make_judge_agent") as mock_factory:
                mock_agent = MagicMock()
                mock_agent.run = AsyncMock(return_value=fake_result)
                mock_factory.return_value = mock_agent

                result = await eval_service.judge_triplet(self._make_row(), "claude-haiku-4-5-20251001")
        finally:
            eval_service.ANTHROPIC_KEY = orig_key

        self.assertEqual(result["winner"], "none")

    async def test_returns_none_on_agent_exception(self):
        orig_key = eval_service.ANTHROPIC_KEY
        eval_service.ANTHROPIC_KEY = "sk-test"
        try:
            with patch("eval_service._make_judge_agent") as mock_factory:
                mock_agent = MagicMock()
                mock_agent.run = AsyncMock(side_effect=Exception("timeout"))
                mock_factory.return_value = mock_agent

                result = await eval_service.judge_triplet(self._make_row(), "claude-haiku-4-5-20251001")
        finally:
            eval_service.ANTHROPIC_KEY = orig_key

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
