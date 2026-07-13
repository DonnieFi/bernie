"""Tests for Phase 26-04 — ResearchWorker + request_research tool."""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

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


class TestSearchHelper(unittest.IsolatedAsyncioTestCase):
    async def test_searxng_returns_urls(self):
        from cognitive_workers.research_io import searxng_search
        fake_body = {"results": [
            {"url": "https://a.example/x", "title": "A"},
            {"url": "https://b.example/y", "title": "B"},
        ]}
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value=fake_body)
        get_cm = MagicMock()
        get_cm.__aenter__ = AsyncMock(return_value=resp)
        get_cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=get_cm)

        urls = await searxng_search(session, "http://searxng.lan:8081", "topic", limit=2)
        self.assertEqual(urls, ["https://a.example/x", "https://b.example/y"])

    async def test_searxng_handles_empty(self):
        from cognitive_workers.research_io import searxng_search
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={"results": []})
        get_cm = MagicMock(__aenter__=AsyncMock(return_value=resp),
                           __aexit__=AsyncMock(return_value=None))
        session = MagicMock()
        session.get = MagicMock(return_value=get_cm)
        urls = await searxng_search(session, "http://x", "q", limit=5)
        self.assertEqual(urls, [])

    async def test_searxng_non_200_returns_empty(self):
        from cognitive_workers.research_io import searxng_search
        resp = MagicMock(); resp.status = 500
        resp.json = AsyncMock(return_value={})
        get_cm = MagicMock(__aenter__=AsyncMock(return_value=resp),
                           __aexit__=AsyncMock(return_value=None))
        session = MagicMock()
        session.get = MagicMock(return_value=get_cm)
        urls = await searxng_search(session, "http://x", "q")
        self.assertEqual(urls, [])


@unittest.skipUnless(db is not None, "database not available")
class TestResearchWorkerCaps(unittest.IsolatedAsyncioTestCase):
    """ResearchWorker MUST respect max_iterations even if depth requested is higher."""

    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "research.db")
        await db.init_db()
        self.container = MagicMock()
        self.container.db = db

    async def asyncTearDown(self):
        if hasattr(db, 'close_db'):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_iteration_cap_enforced(self):
        from cognitive_workers.research import ResearchWorker
        cfg = {
            "ollama_base_url": "http://x",
            "searxng_url": "http://searxng.lan:8081",
            "cognitive_workers": {"research": {
                "default_model": "qwen2.5:14b", "upgrade_model": None,
                "escalate_above_tokens": 9999, "num_ctx": 8192,
                "max_runtime_s": 60, "max_iterations": 2, "max_urls_per_iteration": 1,
            }},
        }
        calls = {"queries": 0, "summaries": 0, "synth": 0}

        async def fake_ollama(model, prompt, config, num_ctx=None, system=None, timeout_s=120):
            stats = {"model": model, "tokens_in": 1, "tokens_out": 1, "duration_ms": 1, "gpu_ms": 1}
            if system and "research planner" in system:
                calls["queries"] += 1
                # Phase 28.5 §4: prompt now requires {"queries": [...]} object form.
                return ('{"queries":["topic phase 26"]}', stats)
            if system and "Summarise" in system:
                calls["summaries"] += 1
                return ("finding text", stats)
            if system and "Synthesise" in system:
                calls["synth"] += 1
                return ("# Final\nAll good.", stats)
            return ("noop", stats)

        async def fake_search(session, base, q, limit=5, timeout_s=8):
            return ["https://x.example"]

        async def fake_fetch(session, urls, concurrency=3, timeout_s=20, max_chars_per_doc=8000):
            return [(urls[0], "page content")]

        task_id = await db.create_cognitive_task(
            type="research", payload={"topic": "x", "requester_id": "dad", "depth": 3},
            actor_id="123", channel_id="456",
        )
        task = {"id": task_id, "type": "research", "actor_id": "123", "channel_id": "456",
                "payload": {"topic": "x", "requester_id": "dad", "depth": 3}}

        mock_session = MagicMock()
        with patch("worker._call_ollama_topic", side_effect=fake_ollama), \
             patch("cognitive_workers.research_io.searxng_search", side_effect=fake_search), \
             patch("cognitive_workers.research_io.fetch_many", side_effect=fake_fetch), \
             patch("cognitive_workers.research.get_http_session", return_value=mock_session):
            result = await ResearchWorker(cfg).handle(task, self.container)

        # depth=3 requested but max_iterations=2 cap applies
        self.assertEqual(calls["queries"], 2)
        self.assertEqual(calls["summaries"], 2)
        self.assertEqual(calls["synth"], 1)
        self.assertIn("_stats", result)

        # Output stored + delivery task enqueued
        out = await db.get_task_output_by_key(f"research:{task_id}")
        self.assertIsNotNone(out)
        self.assertIn("Final", out["content"])
        async with sqlite_async.connect(db.DB_PATH) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM cognitive_tasks WHERE type='research_deliver'"
            )
            self.assertEqual((await cur.fetchone())[0], 1)


class TestResearchQueriesTypedParsing(unittest.TestCase):
    """Phase 28.5 §4: _safe_json_array deleted in favour of
    agent_utils.parse_typed(text, ResearchQueries). These tests confirm
    the typed-parsing path covers the same robustness scenarios — chatty
    preamble, markdown fences, cap-of-3 — without the dedicated function.
    Parser edge cases (general fence stripping, balanced-brace extraction)
    live in test_agent_utils.py."""

    def test_clean_object_form(self):
        from agent_utils import parse_typed
        from typed_outputs import ResearchQueries
        q = parse_typed('{"queries":["a","b"]}', ResearchQueries)
        self.assertIsNotNone(q)
        self.assertEqual(q.queries, ["a", "b"])

    def test_markdown_fence_stripped(self):
        from agent_utils import parse_typed
        from typed_outputs import ResearchQueries
        q = parse_typed('```json\n{"queries":["a","b","c"]}\n```', ResearchQueries)
        self.assertIsNotNone(q)
        self.assertEqual(q.queries, ["a", "b", "c"])

    def test_chatty_preamble_extracted(self):
        from agent_utils import parse_typed
        from typed_outputs import ResearchQueries
        text = (
            "Here are some queries to try:\n"
            '{"queries":["best kayaks halifax","electric kayak reviews"]}\n'
            "Let me know!"
        )
        q = parse_typed(text, ResearchQueries)
        self.assertIsNotNone(q)
        self.assertEqual(q.queries, ["best kayaks halifax", "electric kayak reviews"])

    def test_caps_at_three(self):
        # Pydantic Field(max_length=3) — anything beyond 3 fails validation.
        # Caller (ResearchWorker.handle) sees None and retries with feedback;
        # if retry still fails, the iteration stops naturally on empty queries.
        from agent_utils import parse_typed
        from typed_outputs import ResearchQueries
        self.assertIsNone(parse_typed('{"queries":["a","b","c","d","e"]}', ResearchQueries))

    def test_empty_returns_none(self):
        from agent_utils import parse_typed
        from typed_outputs import ResearchQueries
        self.assertIsNone(parse_typed("", ResearchQueries))
        self.assertIsNone(parse_typed("just prose no JSON anywhere", ResearchQueries))

    def test_bare_array_returns_none(self):
        # The OLD _safe_json_array accepted bare arrays ["a","b"]. The NEW
        # typed form requires {"queries": [...]} object wrapping (matches the
        # other workers + updated QUERY_SYSTEM prompt). This test pins the
        # behaviour change; if the model emits a bare array, retry-with-
        # feedback handles it via validation_error_summary.
        from agent_utils import parse_typed
        from typed_outputs import ResearchQueries
        self.assertIsNone(parse_typed('["a","b"]', ResearchQueries))


@unittest.skipUnless(db is not None, "database not available")
class TestResearchWorkerQueryRetry(unittest.IsolatedAsyncioTestCase):
    """Phase 28.5 §4: when the model's first query response fails validation,
    the worker retries once with the error message appended and continues
    the iteration with the corrected output. Mirrors §2/§3 retry pattern."""

    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self._tmpdir.name, "research_retry.db")
        await db.init_db()
        self.container = MagicMock()
        self.container.db = db

    async def asyncTearDown(self):
        if hasattr(db, 'close_db'):
            await db.close_db()
        db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_query_retry_recovers_then_continues(self):
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
        # Iteration sequence:
        #   1. queries call → BAD output (bare array, no "queries" key)
        #   2. retry queries call → GOOD output (object form)
        #   3. summary call → finding text
        #   4. synth call → final markdown
        call_log = []

        async def fake_ollama(model, prompt, config, num_ctx=None, system=None, timeout_s=120):
            stats = {"model": model, "tokens_in": 10, "tokens_out": 5, "duration_ms": 50, "gpu_ms": 40}
            if system and "research planner" in system:
                call_log.append("query")
                if call_log.count("query") == 1:
                    # First attempt: bare array (old format) — fails validation
                    return ('["bad bare array"]', stats)
                # Retry: correct object form
                return ('{"queries":["recovered query"]}', stats)
            if system and "Summarise" in system:
                call_log.append("summary")
                return ("findings ok", stats)
            if system and "Synthesise" in system:
                call_log.append("synth")
                return ("# Synth\nDone.", stats)
            return ("noop", stats)

        async def fake_search(session, base, q, limit=5, timeout_s=8):
            # Confirms the *retry* query string reached the search step.
            self.assertEqual(q, "recovered query")
            return ["https://x.example"]

        async def fake_fetch(session, urls, **kw):
            return [(urls[0], "page body")]

        task_id = await db.create_cognitive_task(
            type="research", payload={"topic": "x", "requester_id": "dad"},
            actor_id="123", channel_id="456",
        )
        task = {"id": task_id, "type": "research", "actor_id": "123", "channel_id": "456",
                "payload": {"topic": "x", "requester_id": "dad"}}

        mock_session = MagicMock()
        with patch("worker._call_ollama_topic", side_effect=fake_ollama), \
             patch("cognitive_workers.research_io.searxng_search", side_effect=fake_search), \
             patch("cognitive_workers.research_io.fetch_many", side_effect=fake_fetch), \
             patch("cognitive_workers.research.get_http_session", return_value=mock_session), \
             self.assertLogs("bernie.research", level="WARNING") as logs:
            result = await ResearchWorker(cfg).handle(task, self.container)

        # Two query calls (initial + retry), one summary, one synth.
        self.assertEqual(call_log.count("query"), 2)
        self.assertEqual(call_log.count("summary"), 1)
        self.assertEqual(call_log.count("synth"), 1)
        self.assertTrue(any("validation failed" in m for m in logs.output))
        # The synthesis ran and produced output.
        self.assertIn("_stats", result)
        out = await db.get_task_output_by_key(f"research:{task_id}")
        self.assertIsNotNone(out)
        self.assertIn("Synth", out["content"])

    async def test_query_retry_both_fail_stops_iteration(self):
        """If both attempts fail, the iteration ends naturally on empty queries
        and synthesis runs with no findings. The OLD silent-empty meant a single
        bad query response would just stop iteration; new behaviour logs a
        WARNING so the failure is observable."""
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
        call_log = []

        async def fake_ollama(model, prompt, config, num_ctx=None, system=None, timeout_s=120):
            stats = {"model": model, "tokens_in": 5, "tokens_out": 2, "duration_ms": 30, "gpu_ms": 20}
            if system and "research planner" in system:
                call_log.append("query")
                return ("garbage no JSON at all", stats)
            if system and "Synthesise" in system:
                call_log.append("synth")
                return ("# Synth\nNothing found.", stats)
            return ("noop", stats)

        task_id = await db.create_cognitive_task(
            type="research", payload={"topic": "x", "requester_id": "dad"},
            actor_id="123", channel_id="456",
        )
        task = {"id": task_id, "type": "research", "actor_id": "123", "channel_id": "456",
                "payload": {"topic": "x", "requester_id": "dad"}}

        mock_session = MagicMock()
        with patch("worker._call_ollama_topic", side_effect=fake_ollama), \
             patch("cognitive_workers.research.get_http_session", return_value=mock_session), \
             self.assertLogs("bernie.research", level="WARNING") as logs:
            await ResearchWorker(cfg).handle(task, self.container)

        # Two query calls (initial + retry, both garbage), then iteration ends
        # and synth runs once on the empty findings set.
        self.assertEqual(call_log.count("query"), 2)
        self.assertEqual(call_log.count("synth"), 1)
        self.assertTrue(any("validation failed" in m for m in logs.output))


class TestRequestResearchTool(unittest.TestCase):
    """request_research must be registered via @tool and exposed to all roles."""

    def test_tool_defined(self):
        from tools import ROLE_ALL, get_registry
        from tool_gateway import ToolGateway

        import tools.cognitive  # noqa: F401 — triggers @tool registration

        reg = get_registry()
        self.assertIn("request_research", reg)
        entry = reg["request_research"]
        self.assertEqual(entry["name"], "request_research")
        self.assertEqual(entry["role_required"], ROLE_ALL)
        self.assertTrue(entry["is_write"])
        self.assertIs(entry["fn"], tools.cognitive.handle_request_research)

        gw = ToolGateway(registry=reg)
        family_tools = {t["name"] for t in gw.get_tool_schemas("family", cal_available=True)}
        self.assertIn("request_research", family_tools)

        # System prompt mention (context blocks, not claude_service facade)
        with open(os.path.join(os.path.dirname(__file__), "..", "context.py")) as f:
            ctx_src = f.read()
        self.assertIn("request_research", ctx_src)
        # Worker names live in static invariant rules (not class imports)
        self.assertIn("Reflection", ctx_src)
        self.assertIn("MemoryConsolidation", ctx_src)


if __name__ == "__main__":
    unittest.main()
