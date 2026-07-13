"""Tests covering the fixes from the post-Phase-27 code review.

All tests use unittest (no pytest) and avoid network / Discord / Anthropic
calls — handlers are exercised against in-memory fakes.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_ctx(*, group="family", shadow=False, config=None, orchestrator=None,
              person_id="dad", unified_tasks=None, task_store=None):
    from executor import ServiceRefs, ToolContext
    return ToolContext(
        config=config or {},
        person_id=person_id,
        group=group,
        channel_id=None,
        shadow=shadow,
        executor="native",
        services=ServiceRefs(
            calendar=None, ha=None, db=None, session=None,
            orchestrator=orchestrator, identity=None, tz=None,
            unified_tasks=unified_tasks,
            task_store=task_store,
        ),
    )


def _services_with_llm_client(fake_client):
    from executor import ServiceRefs
    return ServiceRefs(llm_for=lambda _model: fake_client)


# ── Critical #1: chat_meal_planning no longer NameErrors on person_id ────────

class TestChatMealPlanningPersonId(unittest.TestCase):
    """`chat_meal_planning` used to reference an undefined `person_id` and
    blow up on every #furnace call. The fix resolves it from `person_name`."""

    def test_passes_resolved_person_id_to_run_loop(self):
        import importlib
        chat_mod = importlib.import_module("llm.chat")
        from llm.chat import chat_meal_planning

        captured: dict = {}

        async def fake_run_loop(*args, **kwargs):
            captured.update(kwargs)
            return "ok"

        async def fake_close(_client):
            return None

        from constants import registry as person_registry

        orig_run_loop = chat_mod._run_loop
        orig_make_client = chat_mod._make_client
        orig_close_client = chat_mod._close_client
        orig_resolve = person_registry.resolve

        chat_mod._run_loop = fake_run_loop
        chat_mod._make_client = lambda base_url=None: MagicMock()
        chat_mod._close_client = fake_close
        person_registry.resolve = lambda name: "dad" if name else None
        try:
            asyncio.run(chat_meal_planning(
                user_message="What's for dinner?",
                history=[],
                config={"timezone": "America/Halifax"},
                person_name="Dad",
            ))
        finally:
            chat_mod._run_loop = orig_run_loop
            chat_mod._make_client = orig_make_client
            chat_mod._close_client = orig_close_client
            person_registry.resolve = orig_resolve

        self.assertEqual(captured.get("person_id"), "dad")
        # actor_id falls back to person_id when no explicit actor_id passed
        self.assertEqual(captured.get("actor_id"), "dad")

    def test_no_person_name_leaves_person_id_none(self):
        import importlib
        chat_mod = importlib.import_module("llm.chat")
        from llm.chat import chat_meal_planning

        captured: dict = {}

        async def fake_run_loop(*args, **kwargs):
            captured.update(kwargs)
            return "ok"

        async def fake_close(_client):
            return None

        orig_run_loop = chat_mod._run_loop
        orig_make_client = chat_mod._make_client
        orig_close_client = chat_mod._close_client

        chat_mod._run_loop = fake_run_loop
        chat_mod._make_client = lambda base_url=None: MagicMock()
        chat_mod._close_client = fake_close
        try:
            asyncio.run(chat_meal_planning(
                user_message="What's for dinner?",
                history=[],
                config={"timezone": "America/Halifax"},
                person_name=None,
            ))
        finally:
            chat_mod._run_loop = orig_run_loop
            chat_mod._make_client = orig_make_client
            chat_mod._close_client = orig_close_client

        self.assertIsNone(captured.get("person_id"))


# ── Critical #2: decline_task uses ctx.services.orchestrator ─────────────────

class TestDeclineTaskOrchestrator(unittest.TestCase):
    """decline_task previously imported a non-existent `router` symbol from
    `notification_router`. It should use ctx.services.orchestrator like the
    other write handlers."""

    def test_uses_injected_orchestrator(self):
        import database as db
        from tools import load_all_domains, get_registry
        load_all_domains()
        handler = get_registry()["decline_task"]["fn"]

        fake_task = {
            "id": 42,
            "title": "Take out trash",
            "assigned_by": "child1",
        }

        fake_unified = MagicMock()
        fake_unified.decline_task = AsyncMock(return_value=None)
        fake_task_store = MagicMock()
        fake_task_store.get_task = AsyncMock(return_value=fake_task)

        fake_orchestrator = MagicMock()
        fake_orchestrator.notify = AsyncMock(return_value=None)

        ctx = _make_ctx(
            orchestrator=fake_orchestrator,
            unified_tasks=fake_unified,
            task_store=fake_task_store,
        )
        result = asyncio.run(handler(
            {"task_id": 42, "reason": "too busy"}, ctx,
        ))

        self.assertIn("declined", result.lower())
        fake_unified.decline_task.assert_awaited_once()

    def test_no_router_import_from_notification_router(self):
        """Defensive: notification_router.py must not export a top-level
        `router` (the missing symbol that caused the original ImportError)."""
        import notification_router
        self.assertFalse(
            hasattr(notification_router, "router"),
            "notification_router.router resurrected — decline_task would silently couple to it again",
        )


# ── Critical #3: list-returning tools don't crash NativeToolExecutor ─────────

class TestNativeExecutorHandlesNonStringResult(unittest.TestCase):
    """The validation-failure check used to do `result.startswith(...)` on
    whatever the gateway returned, which crashed for tools that return list
    content (e.g. get_camera_snapshot). The new path catches ToolValidationError
    explicitly so non-string results pass through untouched."""

    def test_list_result_does_not_raise(self):
        from executor import ExecutorConfig
        from executors.native import NativeToolExecutor

        gw = MagicMock()
        gw.execute = AsyncMock(return_value=[
            {"type": "text", "text": "snapshot"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "..."}},
        ])

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "get_camera_snapshot"
        tool_block.input = {"camera": "cam_18"}
        tool_block.id = "tool_use_1"

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Here's the camera."

        first_resp = MagicMock()
        first_resp.stop_reason = "tool_use"
        first_resp.content = [tool_block]
        first_resp.usage = MagicMock(input_tokens=10, output_tokens=5,
                                     cache_creation_input_tokens=0, cache_read_input_tokens=0)

        second_resp = MagicMock()
        second_resp.stop_reason = "end_turn"
        second_resp.content = [text_block]
        second_resp.usage = first_resp.usage

        responses = [first_resp, second_resp]

        async def fake_create(**_kw):
            return responses.pop(0)

        fake_client = MagicMock()
        fake_client.messages.create = AsyncMock(side_effect=fake_create)

        ne = NativeToolExecutor(gateway=gw).with_services(_services_with_llm_client(fake_client))
        cfg = ExecutorConfig(surface="chat", model="claude-sonnet-4-6")
        result = asyncio.run(ne.run([{"role": "user", "content": "show me"}], "sys", [], cfg))

        self.assertEqual(result, "Here's the camera.")
        gw.execute.assert_awaited_once()


# ── Warning #7: ToolValidationError drives retry/escalation ──────────────────

class TestValidationErrorPlumbing(unittest.TestCase):
    def test_gateway_raises_validation_error(self):
        from tool_gateway import ToolGateway, ToolValidationError
        from tools import load_all_domains, get_registry
        load_all_domains()
        gw = ToolGateway(registry=get_registry())

        # `create_event` requires summary/date/time; pass only summary.
        ctx = _make_ctx(group="all", shadow=True)
        with self.assertRaises(ToolValidationError) as cm:
            asyncio.run(gw.execute("create_event", {"summary": "x"}, ctx))
        self.assertEqual(cm.exception.tool_name, "create_event")
        self.assertIn("Invalid arguments", cm.exception.message)

    def test_native_executor_counts_validation_failures_and_escalates(self):
        from executor import ExecutorConfig
        from executors.native import NativeToolExecutor
        from tool_gateway import ToolValidationError

        gw = MagicMock()

        async def boom(*_a, **_kw):
            raise ToolValidationError("create_event", "Invalid arguments for 'create_event'")

        gw.execute = boom

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "create_event"
        tool_block.input = {}
        tool_block.id = "id1"

        end_block = MagicMock()
        end_block.text = "okay"

        first = MagicMock(stop_reason="tool_use", content=[tool_block, tool_block])
        first.usage = MagicMock(input_tokens=0, output_tokens=0,
                                cache_creation_input_tokens=0, cache_read_input_tokens=0)
        last = MagicMock(stop_reason="end_turn", content=[end_block])
        last.usage = first.usage
        responses = [first, last]

        models_used: list[str] = []

        async def fake_create(**kw):
            models_used.append(kw["model"])
            return responses.pop(0)

        fake_client = MagicMock()
        fake_client.messages.create = AsyncMock(side_effect=fake_create)

        ne = NativeToolExecutor(gateway=gw).with_services(_services_with_llm_client(fake_client))

        from config import config as app_config
        _sentinel = object()
        had_key = "primary_reliable_model" in app_config
        old_val = app_config.get("primary_reliable_model", _sentinel)
        app_config["primary_reliable_model"] = "claude-sonnet-4-6"
        try:
            cfg = ExecutorConfig(surface="chat", model="or-something-flaky")
            asyncio.run(ne.run([{"role": "user", "content": "hi"}], "", [], cfg))
        finally:
            if had_key:
                app_config["primary_reliable_model"] = old_val
            else:
                app_config.pop("primary_reliable_model", None)

        # First call uses the original (flaky) model; after 2 validation failures
        # in one turn, the next iteration switches to the reliable model.
        self.assertEqual(models_used[0], "or-something-flaky")
        self.assertEqual(models_used[-1], "claude-sonnet-4-6")


# ── Warning #4: SmolExecutor model adapter preserves system prompts ──────────

class TestSmolModelSystemPrompt(unittest.TestCase):
    def test_system_messages_routed_via_system_param(self):
        from executors.smol import _AnthropicSmolModel

        captured = {}

        class _SpyClient:
            class messages:
                @staticmethod
                def create(**kwargs):
                    captured.update(kwargs)
                    # Return a stub matching the Anthropic SDK shape.
                    blk = type("B", (), {"text": "ok"})()
                    return type("R", (), {"content": [blk]})()

        model = _AnthropicSmolModel(model_id="claude-sonnet-4-6")
        model._client = _SpyClient()
        result = model.generate([
            {"role": "system", "content": "CodeAgent scaffolding here"},
            {"role": "system", "content": "Another system note"},
            {"role": "user",   "content": "Do the thing"},
        ])
        self.assertEqual(result.content, "ok")
        # Both system messages joined and forwarded via `system=`
        self.assertIn("CodeAgent scaffolding here", captured["system"])
        self.assertIn("Another system note", captured["system"])
        # User message went through as a regular message.
        roles = [m["role"] for m in captured["messages"]]
        self.assertEqual(roles, ["user"])

    def test_no_system_messages_omits_system_param(self):
        from executors.smol import _AnthropicSmolModel

        captured = {}

        class _SpyClient:
            class messages:
                @staticmethod
                def create(**kwargs):
                    captured.update(kwargs)
                    blk = type("B", (), {"text": "ok"})()
                    return type("R", (), {"content": [blk]})()

        model = _AnthropicSmolModel(model_id="claude-sonnet-4-6")
        model._client = _SpyClient()
        model.generate([{"role": "user", "content": "hi"}])
        self.assertNotIn("system", captured,
                         "system param must be omitted when no system messages are present")


# ── Hotfix: request_research validates requester_id is a Discord snowflake ──

class TestRequestResearchRequesterValidation(unittest.TestCase):
    """When the user says 'email me the results', models routinely pass the
    email as `requester_id` despite the description saying 'Discord ID'.
    The deliver step then notifies an invalid recipient and the synthesis
    is stored but never delivered. Validate snowflake format and fall back
    to the resolved caller."""

    def _patch_registry(self, person_id_to_discord: dict):
        from constants import registry as person_registry
        orig_resolve = person_registry.resolve
        orig_get = person_registry.get

        person_registry.resolve = lambda name: name if name in person_id_to_discord else None
        person_registry.get = lambda pid: (
            {"discord_id": person_id_to_discord[pid]} if pid in person_id_to_discord else None
        )

        def restore():
            person_registry.resolve = orig_resolve
            person_registry.get = orig_get

        return restore

    def _patch_db(self):
        import db_writes

        created: list = []

        async def fake_routed(op, /, *args, **kwargs):
            if op == "create_cognitive_task":
                created.append(kwargs)
                return 999
            return None

        orig = db_writes.routed
        db_writes.routed = fake_routed

        def restore():
            db_writes.routed = orig

        return created, restore

    def test_email_as_requester_id_falls_back_to_caller(self):
        import asyncio
        from tools import load_all_domains, get_registry
        load_all_domains()
        handler = get_registry()["request_research"]["fn"]

        restore_reg = self._patch_registry({"dad": "111222333"})
        created, restore_db = self._patch_db()
        try:
            ctx = _make_ctx(person_id="dad")
            result = asyncio.run(handler(
                {"topic": "Lisbon hotels", "requester_id": "parent@example.com"}, ctx,
            ))
        finally:
            restore_db()
            restore_reg()

        self.assertEqual(len(created), 1)
        # The enqueued task uses the resolved caller, not the email
        self.assertEqual(created[0]["payload"]["requester_id"], "111222333")
        self.assertEqual(created[0]["channel_id"], "111222333")
        self.assertIn("queued", result.lower())

    def test_digit_requester_id_passes_through(self):
        import asyncio
        from tools import load_all_domains, get_registry
        load_all_domains()
        handler = get_registry()["request_research"]["fn"]

        restore_reg = self._patch_registry({"dad": "111222333"})
        created, restore_db = self._patch_db()
        try:
            ctx = _make_ctx(person_id="dad")
            asyncio.run(handler(
                {"topic": "X", "requester_id": "555666777"}, ctx,
            ))
        finally:
            restore_db()
            restore_reg()

        self.assertEqual(created[0]["payload"]["requester_id"], "555666777")

    def test_missing_requester_id_uses_caller(self):
        import asyncio
        from tools import load_all_domains, get_registry
        load_all_domains()
        handler = get_registry()["request_research"]["fn"]

        restore_reg = self._patch_registry({"dad": "111222333"})
        created, restore_db = self._patch_db()
        try:
            ctx = _make_ctx(person_id="dad")
            asyncio.run(handler({"topic": "X"}, ctx))
        finally:
            restore_db()
            restore_reg()

        self.assertEqual(created[0]["payload"]["requester_id"], "111222333")


# ── Hotfix: defer_response no longer spawns a hallucinating phi4 task ───────

class TestDeferResponseIsAckOnly(unittest.TestCase):
    """Production regression caught in Langfuse trace ffd4e1454739414787da6f5e8df9ab41:
    user asked Bernie to research Lisbon hotels. Bernie correctly fired
    `request_research` AND `defer_response` (per the system prompt). The
    `defer_response` enqueued a `discord_reply` task; the worker handed
    the topic to phi4 (no tools, no fetched pages); phi4 produced
    'I'm unable to research or send emails as I do not have internet
    access…' and DM'd it to the user, racing the real synthesis.

    Fix: defer_response is acknowledgement-only — no background task."""

    def test_defer_response_does_not_create_cognitive_task(self):
        import asyncio
        from tools import load_all_domains, get_registry
        load_all_domains()
        handler = get_registry()["defer_response"]["fn"]

        import database as db
        created_types: list = []

        async def fake_create_task(**kwargs):
            created_types.append(kwargs.get("type"))
            return 999

        orig_create = db.create_cognitive_task
        db.create_cognitive_task = fake_create_task
        try:
            ctx = _make_ctx(person_id="dad")
            result = asyncio.run(handler(
                {"topic": "Research Lisbon hotels", "acknowledgement": "On it!"}, ctx,
            ))
        finally:
            db.create_cognitive_task = orig_create

        self.assertEqual(result, "On it!", "must echo the acknowledgement verbatim")
        self.assertEqual(
            created_types, [],
            f"defer_response must not enqueue any cognitive task — got: {created_types}",
        )

    def test_defer_response_shadow_still_blocks(self):
        import asyncio
        from tools import load_all_domains, get_registry
        load_all_domains()
        handler = get_registry()["defer_response"]["fn"]
        ctx = _make_ctx(shadow=True, person_id="dad")
        result = asyncio.run(handler(
            {"topic": "anything", "acknowledgement": "On it!"}, ctx,
        ))
        self.assertIn("shadow", result.lower())

    def test_defer_response_rejects_empty_topic(self):
        import asyncio
        from tools import load_all_domains, get_registry
        load_all_domains()
        handler = get_registry()["defer_response"]["fn"]
        ctx = _make_ctx(person_id="dad")
        result = asyncio.run(handler(
            {"topic": "", "acknowledgement": "ack"}, ctx,
        ))
        self.assertIn("topic is required", result.lower())


# Native LLM routing now lives in ServiceContainer.llm_for — see
# bot/tests/test_llm_routing.py and bot/tests/test_model_selection.py.


# ── Warning #6: ToolGateway tracks background tasks ──────────────────────────

class TestBackgroundTaskTracking(unittest.TestCase):
    def test_bg_tasks_held_and_discarded(self):
        from tool_gateway import ToolGateway

        gw = ToolGateway(registry={})

        async def _drive():
            release = asyncio.Event()

            async def slow():
                await release.wait()

            gw._spawn_bg(slow())
            self.assertEqual(len(gw._bg_tasks), 1, "task should be retained")
            release.set()
            # Let the task run to completion.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(len(gw._bg_tasks), 0, "task should self-discard")

        asyncio.run(_drive())

    def test_spawn_bg_without_loop_closes_coroutine(self):
        from tool_gateway import ToolGateway

        gw = ToolGateway(registry={})

        async def never_run():
            return None

        coro = never_run()
        gw._spawn_bg(coro)   # no running loop here
        # If the coroutine wasn't closed, Python emits a "coroutine was never
        # awaited" RuntimeWarning. We rely on no exception being raised.
        self.assertEqual(len(gw._bg_tasks), 0)


# ── Warning #15: shadow_leg key used by activity log + query ─────────────────

class TestShadowLegKey(unittest.TestCase):
    def test_activity_meta_carries_shadow_leg_key(self):
        from tool_gateway import ToolGateway

        captured: dict = {}

        async def fake_routed(op, /, *args, **kwargs):
            if op == "log_activity":
                captured.update(kwargs)

        import db_writes
        orig = db_writes.routed
        db_writes.routed = fake_routed
        try:
            ctx_primary = _make_ctx(shadow=False)
            ctx_primary.services.db = object()
            ctx_harness = _make_ctx(shadow=True)
            ctx_harness.services.db = object()
            gw = ToolGateway(registry={})

            asyncio.run(gw._emit_activity("ping", ctx_primary))
            self.assertIn("shadow_leg=primary", captured["meta"])

            captured.clear()
            asyncio.run(gw._emit_activity("ping", ctx_harness))
            self.assertIn("shadow_leg=harness", captured["meta"])
        finally:
            db_writes.routed = orig


# ── Suggestion: @tool decorator stamps a domain field ────────────────────────

class TestToolDecoratorDomain(unittest.TestCase):
    def test_calendar_handlers_have_domain_calendar(self):
        from tools import get_registry, load_all_domains
        load_all_domains()
        reg = get_registry()
        self.assertEqual(reg["get_todays_events"]["domain"], "calendar")
        self.assertEqual(reg["create_event"]["domain"], "calendar")

    def test_get_tool_schemas_filters_by_domain_not_module_path(self):
        from tools import get_registry, load_all_domains
        from tool_gateway import ToolGateway
        load_all_domains()
        gw = ToolGateway(registry=get_registry())
        names_off = {t["name"] for t in gw.get_tool_schemas("family", cal_available=False)}
        names_on = {t["name"] for t in gw.get_tool_schemas("family", cal_available=True)}
        self.assertIn("get_todays_events", names_on)
        self.assertNotIn("get_todays_events", names_off)


if __name__ == "__main__":
    unittest.main()
