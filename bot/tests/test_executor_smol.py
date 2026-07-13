"""SmolExecutor: Protocol conformance and tool wrapper tests."""
import asyncio
import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from executor import Executor, ServiceRefs, ExecutorConfig


class TestSmolExecutorProtocol(unittest.TestCase):
    def setUp(self):
        from tool_gateway import ToolGateway
        from tools import get_registry, load_all_domains
        load_all_domains()
        self.gw = ToolGateway(registry=get_registry())
        self.services = ServiceRefs(
            calendar=None, ha=None, db=None, session=None,
            orchestrator=None, identity=None, tz=None,
        )

    def _family_tools(self):
        """Return a filtered family-group tool schema list (no admin tools)."""
        return self.gw.get_tool_schemas("family")

    def test_conforms_to_executor_protocol(self):
        from executors.smol import SmolExecutor
        se = SmolExecutor(gateway=self.gw).with_services(self.services)
        self.assertIsInstance(se, Executor)

    def test_tool_wrappers_built_from_filtered_schema(self):
        from executors.smol import SmolExecutor
        se = SmolExecutor(gateway=self.gw).with_services(self.services)
        cfg = ExecutorConfig(surface="chat", model="claude-sonnet-4-6", shadow=True)
        tools = self._family_tools()

        async def _run():
            return se._build_tool_wrappers(tools, cfg)

        wrappers = asyncio.run(_run())
        self.assertGreater(len(wrappers), 0)
        names = {w.name for w in wrappers}
        self.assertIn("get_todays_events", names)
        self.assertIn("control_device", names)

    def test_wrapper_names_match_filtered_schema_not_full_registry(self):
        """Wrappers must reflect the filtered tool list, not the full registry."""
        from executors.smol import SmolExecutor
        from tools import get_registry
        se = SmolExecutor(gateway=self.gw).with_services(self.services)
        cfg = ExecutorConfig(surface="chat", model="claude-sonnet-4-6", shadow=True)
        tools = self._family_tools()

        async def _run():
            return se._build_tool_wrappers(tools, cfg)

        wrappers = asyncio.run(_run())
        wrapper_names = {w.name for w in wrappers}
        schema_names = {t["name"] for t in tools}
        self.assertEqual(wrapper_names, schema_names)

        # Admin-only tools must NOT appear in the family-group wrappers
        all_names = set(get_registry().keys())
        extra = wrapper_names - schema_names
        self.assertEqual(extra, set(), f"Wrappers contain tools outside the filtered schema: {extra}")

    def test_admin_tools_absent_from_family_wrappers(self):
        """RBAC filtered list must not leak admin tools into family-group wrappers."""
        from executors.smol import SmolExecutor
        se = SmolExecutor(gateway=self.gw).with_services(self.services)
        cfg = ExecutorConfig(surface="chat", model="claude-sonnet-4-6", shadow=True, group="family")
        tools = self.gw.get_tool_schemas("family")

        async def _run():
            return se._build_tool_wrappers(tools, cfg)

        wrappers = asyncio.run(_run())
        wrapper_names = {w.name for w in wrappers}
        admin_tools = self.gw.get_tool_schemas("admin")
        admin_names = {t["name"] for t in admin_tools} - {t["name"] for t in tools}
        leaked = wrapper_names & admin_names
        self.assertEqual(leaked, set(), f"Admin tools leaked into family wrappers: {leaked}")


class TestGatewayToolFactory(unittest.TestCase):
    def test_callable_with_inputs(self):
        from executors.smol import _make_gateway_tool
        calls = []

        def fake_fn(x=None, y=None):
            calls.append((x, y))
            return f"ok:{x}:{y}"

        tool = _make_gateway_tool(
            "fake_tool", "A fake tool",
            {"x": {"type": "string", "description": "first"},
             "y": {"type": "string", "description": "second"}},
            required_params={"x"},  # x is required, y is optional
            sync_fn=fake_fn,
        )
        result = tool(x="hello", y="world")
        self.assertEqual(result, "ok:hello:world")
        self.assertEqual(calls, [("hello", "world")])

    def test_callable_no_inputs(self):
        from executors.smol import _make_gateway_tool

        tool = _make_gateway_tool("no_args", "No arguments", {}, set(), lambda: "done")
        self.assertEqual(tool(), "done")

    def test_required_params_not_nullable(self):
        """Required params must not carry nullable=True in smol_inputs."""
        from executors.smol import _make_gateway_tool
        tool = _make_gateway_tool(
            "t", "test",
            {"req": {"type": "string", "description": "required"},
             "opt": {"type": "string", "description": "optional"}},
            required_params={"req"},
            sync_fn=lambda req=None, opt=None: "ok",
        )
        self.assertNotIn("nullable", tool.inputs["req"])
        self.assertIn("nullable", tool.inputs["opt"])
        self.assertTrue(tool.inputs["opt"]["nullable"])


class TestAnthropicSmolModel(unittest.TestCase):
    def _smol_services(self):
        """ServiceRefs with llm_for routing matching production _make_smol_model."""
        from unittest.mock import MagicMock
        from config import config as app_config

        def llm_for(model_name: str):
            ollama_models = app_config.get("ollama_models") or []
            if model_name.startswith("claude-"):
                client = MagicMock()
                client.base_url = "https://api.anthropic.com"
                client.api_key = "test"
                return client
            if model_name in ollama_models:
                return app_config.get(
                    "ollama_base_url", "http://192.168.1.X:11434"
                )
            client = MagicMock()
            client.base_url = app_config.get("litellm_base_url", "https://litellm.example.local")
            client.api_key = "sk-test"
            return client

        return ServiceRefs(
            calendar=None, ha=None, db=None, session=None,
            orchestrator=None, identity=None, tz=None,
            llm_for=llm_for,
        )

    def test_instantiates_without_anthropic_import(self):
        from executors.smol import _AnthropicSmolModel
        model = _AnthropicSmolModel(model_id="claude-sonnet-4-6")
        self.assertEqual(model.model_id, "claude-sonnet-4-6")
        self.assertIsNone(model._client)  # lazy init

    def test_factory_returns_anthropic_for_claude(self):
        from executors.smol import _make_smol_model, _AnthropicSmolModel
        model = _make_smol_model("claude-sonnet-4-6", self._smol_services())
        self.assertIsInstance(model, _AnthropicSmolModel)

    def test_factory_routes_ollama_direct(self):
        """ollama_models entries bypass LiteLLM and use OpenAIModel against
        ollama_base_url/v1 (the OpenAI-compat surface Ollama exposes)."""
        from config import config as app_config
        ollama_models = app_config.get("ollama_models") or []
        if not ollama_models:
            self.skipTest("config.ollama_models is empty")
        try:
            import openai  # noqa: F401
            from smolagents import OpenAIModel
        except ImportError:
            self.skipTest("openai / smolagents OpenAIModel not installed in this env")

        from executors.smol import _make_smol_model

        model = _make_smol_model(ollama_models[0], self._smol_services())
        self.assertIsInstance(model, OpenAIModel)
        self.assertEqual(model.model_id, ollama_models[0])
        expected_base = (app_config.get("ollama_base_url", "http://192.168.1.X:11434")
                         .rstrip("/") + "/v1")
        # OpenAIModel stores base_url on client_kwargs
        self.assertEqual(model.client_kwargs.get("base_url"), expected_base)

    def test_factory_routes_litellm_alias(self):
        """or-* aliases must route via OpenAIModel against `<litellm_base>/v1`.

        Mirrors the native-executor LiteLLM hotfix — without this test, a
        regression of either factory could ship the same 404-on-anthropic
        bug we just fixed in NativeToolExecutor.
        """
        try:
            import openai  # noqa: F401
            from smolagents import OpenAIModel
        except ImportError:
            self.skipTest("openai / smolagents OpenAIModel not installed in this env")

        from config import config as app_config
        from executors.smol import _make_smol_model

        original_litellm = app_config.get("litellm_base_url")
        original_openrouter = app_config.get("openrouter_direct")
        app_config["litellm_base_url"] = "https://litellm.example.local"
        app_config["openrouter_direct"] = False
        try:
            model = _make_smol_model("or-deepseek-v4", self._smol_services())
        finally:
            if original_litellm is None:
                app_config.pop("litellm_base_url", None)
            else:
                app_config["litellm_base_url"] = original_litellm
            if original_openrouter is None:
                app_config.pop("openrouter_direct", None)
            else:
                app_config["openrouter_direct"] = original_openrouter

        self.assertIsInstance(model, OpenAIModel)
        self.assertEqual(model.model_id, "or-deepseek-v4")
        self.assertEqual(model.client_kwargs.get("base_url"), "https://litellm.example.local/v1")

    def test_generate_empty_messages_returns_done(self):
        """All non-system messages filtered out → short-circuit reply, no API call."""
        from executors.smol import _AnthropicSmolModel

        model = _AnthropicSmolModel(model_id="claude-sonnet-4-6")

        class _Boom:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise AssertionError("API should not be called when all messages are system")

        model._client = _Boom()
        result = model.generate([{"role": "system", "content": "ignored"}])
        self.assertEqual(result.content, "Done!")

    def test_generate_propagates_api_errors(self):
        """API errors must bubble — never swallowed into assistant content."""
        from executors.smol import _AnthropicSmolModel

        model = _AnthropicSmolModel(model_id="claude-sonnet-4-6")

        class _BoomClient:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("simulated 429")

        model._client = _BoomClient()
        with self.assertRaises(RuntimeError):
            model.generate([{"role": "user", "content": "hi"}])


class TestSyncFnErrorPaths(unittest.TestCase):
    """Tool wrapper sync_fn timeout/exception behaviour."""

    def setUp(self):
        from tool_gateway import ToolGateway
        from tools import get_registry, load_all_domains
        load_all_domains()
        self.gw = ToolGateway(registry=get_registry())
        self.services = ServiceRefs(
            calendar=None, ha=None, db=None, session=None,
            orchestrator=None, identity=None, tz=None,
        )

    def test_sync_fn_returns_error_string_on_exception(self):
        """If gateway.execute raises, sync_fn returns a formatted error string."""
        from executors.smol import SmolExecutor

        async def _boom(name, args, ctx):
            raise RuntimeError("kaboom")

        self.gw.execute = _boom  # type: ignore[method-assign]
        se = SmolExecutor(gateway=self.gw).with_services(self.services)
        cfg = ExecutorConfig(surface="chat", model="claude-sonnet-4-6", shadow=False)

        async def _drive():
            wrappers = se._build_tool_wrappers(
                [{"name": "get_todays_events", "description": "x",
                  "input_schema": {"properties": {}, "required": []}}],
                cfg,
            )
            return await asyncio.get_running_loop().run_in_executor(None, lambda: wrappers[0]())

        result = asyncio.run(_drive())
        self.assertIn("Error in get_todays_events", result)
        self.assertIn("kaboom", result)

    def test_sync_fn_cancels_future_on_timeout(self):
        """When the gateway coroutine exceeds the timeout, the future is cancelled."""
        import executors.smol as smol_module
        from executors.smol import SmolExecutor

        cancelled = asyncio.Event()

        async def _slow(name, args, ctx):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "should-not-reach"

        self.gw.execute = _slow  # type: ignore[method-assign]
        se = SmolExecutor(gateway=self.gw).with_services(self.services)
        cfg = ExecutorConfig(surface="chat", model="claude-sonnet-4-6", shadow=False)

        original_timeout = smol_module.SMOL_TOOL_TIMEOUT_SECONDS
        smol_module.SMOL_TOOL_TIMEOUT_SECONDS = 1  # speed up test
        try:
            async def _drive():
                wrappers = se._build_tool_wrappers(
                    [{"name": "get_todays_events", "description": "x",
                      "input_schema": {"properties": {}, "required": []}}],
                    cfg,
                )
                result = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: wrappers[0]()
                )
                # Give the loop a tick to actually process the cancellation
                await asyncio.sleep(0.05)
                return result

            result = asyncio.run(_drive())
            self.assertIn("timed out", result)
            self.assertTrue(cancelled.is_set(), "Slow coroutine was not cancelled on timeout")
        finally:
            smol_module.SMOL_TOOL_TIMEOUT_SECONDS = original_timeout


class TestLangfuseGeneration(unittest.TestCase):
    """Ensure SmolExecutor emits a top-level Langfuse generation span per run."""

    def test_log_smol_generation_invokes_log_generation(self):
        import executors.smol as smol_module
        from executor import ExecutorConfig, ServiceRefs

        calls = []

        async def fake_log_generation(**kwargs):
            calls.append(kwargs)

        # Patch the symbol used inside _log_smol_generation. The helper imports
        # langfuse_logger lazily inside the try block, so we monkey-patch the
        # module attribute that import resolves to.
        import langfuse_logger
        original = langfuse_logger.log_generation
        langfuse_logger.log_generation = fake_log_generation

        # _log_smol_generation ALSO writes to the local token_usage DB via the
        # shared database.db_conn. Under a bare asyncio.run() that global async
        # connection is bound to another event loop and the write hangs, so stub
        # it out — this test only asserts the Langfuse generation call.
        import database as db_mod
        original_log_usage = db_mod.log_token_usage
        async def _noop_log_usage(*args, **kwargs):
            return None
        db_mod.log_token_usage = _noop_log_usage
        try:
            cfg = ExecutorConfig(
                surface="chat",
                model="claude-sonnet-4-6",
                group="family",
                person_id="p123",
                session_id="s456",
                triggered_by="discord",
                shadow=False,
            )
            services = ServiceRefs(
                calendar=None, ha=None, db=None, session=None,
                orchestrator=None, identity=None, tz=None,
            )
            asyncio.run(smol_module._log_smol_generation(
                config=cfg,
                services=services,
                user_input="hi",
                output="hello",
            ))
        finally:
            langfuse_logger.log_generation = original
            db_mod.log_token_usage = original_log_usage

        self.assertEqual(len(calls), 1, "log_generation should be called exactly once")
        kw = calls[0]
        self.assertEqual(kw["model"], "claude-sonnet-4-6")
        self.assertEqual(kw["user_input"], "hi")
        self.assertEqual(kw["output"], "hello")
        self.assertEqual(kw["name"], "smol_chat")
        self.assertEqual(kw["actor_id"], "p123")
        self.assertEqual(kw["session_id"], "s456")
        self.assertEqual(kw["metadata"]["executor"], "smol")
        self.assertIn("smol", kw["tags"])


class TestIdentifierGuard(unittest.TestCase):
    def test_rejects_non_identifier_param(self):
        """Property names that aren't valid Python identifiers must be rejected."""
        from executors.smol import _make_gateway_tool
        with self.assertRaises(ValueError):
            _make_gateway_tool(
                "bad", "tool with hyphen in property name",
                {"bad-name": {"type": "string", "description": "..."}},
                required_params=set(),
                sync_fn=lambda **_: "ok",
            )

    def test_rejects_python_keyword_param(self):
        """Python keywords as property names must be rejected."""
        from executors.smol import _make_gateway_tool
        with self.assertRaises(ValueError):
            _make_gateway_tool(
                "bad_kw", "tool with keyword property name",
                {"class": {"type": "string", "description": "..."}},
                required_params=set(),
                sync_fn=lambda **_: "ok",
            )


from unittest.mock import AsyncMock


class TestSmolTokenAccumulator(unittest.TestCase):
    """S3: _AnthropicSmolModel must accumulate token counts across generate() calls."""

    def test_token_accumulator_sums_across_calls(self):
        """tokens_in_total / tokens_out_total must accumulate across multiple generate() calls."""
        from executors.smol import _AnthropicSmolModel

        model = _AnthropicSmolModel(model_id="claude-sonnet-4-6")

        class _FakeUsage:
            def __init__(self, inp, out):
                self.input_tokens = inp
                self.output_tokens = out
                self.cache_creation_input_tokens = 0
                self.cache_read_input_tokens = 0

        class _FakeContent:
            def __init__(self, text):
                self.type = "text"
                self.text = text

        class _FakeResp:
            def __init__(self, inp, out, text):
                self.usage = _FakeUsage(inp, out)
                self.content = [_FakeContent(text)]
                self.stop_reason = "end_turn"

        call_count = [0]
        responses = [_FakeResp(100, 50, "First answer"), _FakeResp(80, 40, "Second answer")]

        class _FakeClient:
            class messages:
                @staticmethod
                def create(**kwargs):
                    resp = responses[call_count[0]]
                    call_count[0] += 1
                    return resp

        model._client = _FakeClient()

        model.generate([{"role": "user", "content": "first question"}])
        self.assertEqual(model.tokens_in_total, 100)
        self.assertEqual(model.tokens_out_total, 50)

        model.generate([{"role": "user", "content": "second question"}])
        self.assertEqual(model.tokens_in_total, 180, "tokens_in_total should accumulate")
        self.assertEqual(model.tokens_out_total, 90, "tokens_out_total should accumulate")

    def test_run_core_returns_token_tuple(self):
        """_run_core() must return (text, tok_in, tok_out, cc, cr) — not mutate instance attrs."""
        from executors.smol import SmolExecutor, _AnthropicSmolModel
        from executor import ExecutorConfig, ServiceRefs
        from tool_gateway import ToolGateway
        from tools import get_registry, load_all_domains
        load_all_domains()
        gw = ToolGateway(registry=get_registry())
        services = ServiceRefs(
            calendar=None, ha=None, db=None, session=None,
            orchestrator=None, identity=None, tz=None,
        )

        se = SmolExecutor(gateway=gw).with_services(services)
        cfg = ExecutorConfig(surface="chat", model="claude-sonnet-4-6", shadow=False)

        captured = {}

        class _FakeAgent:
            def run(self, task):
                return "agent result"

        class _FakeModel:
            tokens_in_total = 42
            tokens_out_total = 17
            cache_creation_total = 3
            cache_read_total = 8

        async def _run():
            with unittest.mock.patch("smolagents.CodeAgent", return_value=_FakeAgent()), \
                 unittest.mock.patch("executors.smol._make_smol_model", return_value=_FakeModel()), \
                 unittest.mock.patch("executors.smol._log_smol_generation", new=AsyncMock()):
                return await se._run_core(
                    messages=[{"role": "user", "content": "test"}],
                    system="system",
                    tools=[],
                    config=cfg,
                )

        try:
            result = asyncio.run(_run())
        except ImportError:
            self.skipTest("smolagents not installed")
            return

        text, tok_in, tok_out, cc, cr = result
        self.assertEqual(text, "agent result")
        self.assertEqual(tok_in, 42)
        self.assertEqual(tok_out, 17)
        self.assertEqual(cc, 3)
        self.assertEqual(cr, 8)
        # run() must not have set instance attributes
        self.assertFalse(hasattr(se, "_last_run_tokens_in"), "_run_core should not set instance attrs")


class TestExecutorModelRouting(unittest.TestCase):
    """`executor.smol_models` forces the smol runtime for reasoning models that
    misbehave in the single-shot native loop (the or-deepseek-v4 'Done!' bug)."""

    def _route(self, *, model, exec_cfg):
        import claude_service
        import config as config_mod
        from executor import ServiceRefs
        orig = config_mod.config
        try:
            config_mod.config = {"executor": exec_cfg}
            return claude_service._get_executor("chat", ServiceRefs(), model=model)
        finally:
            config_mod.config = orig

    def test_smol_model_forces_smol_runtime(self):
        from executors.smol import SmolExecutor
        ex = self._route(
            model="or-deepseek-v4",
            exec_cfg={"chat": "native", "smol_models": ["or-deepseek-v4"]},
        )
        self.assertIsInstance(ex, SmolExecutor)

    def test_non_listed_model_keeps_surface_default(self):
        from executors.native import NativeToolExecutor
        ex = self._route(
            model="claude-sonnet-4-6",
            exec_cfg={"chat": "native", "smol_models": ["or-deepseek-v4"]},
        )
        self.assertIsInstance(ex, NativeToolExecutor)

    def test_empty_smol_models_is_noop(self):
        from executors.native import NativeToolExecutor
        ex = self._route(model="or-deepseek-v4", exec_cfg={"chat": "native"})
        self.assertIsInstance(ex, NativeToolExecutor)


class TestIntentRouting(unittest.TestCase):
    """Intent-based chat routing: multi-step/compare queries -> smol, simple
    lookups -> native, while smol_models still forces smol regardless."""

    def test_looks_multistep_detects_comparison(self):
        from claude_service import _looks_multistep
        self.assertTrue(_looks_multistep("Compare Oura and Garmin sleep", {}))
        self.assertTrue(_looks_multistep("what's the difference between the two trackers?", {}))

    def test_looks_multistep_detects_chaining_and_aggregation(self):
        from claude_service import _looks_multistep
        self.assertTrue(_looks_multistep("plan dinner around everyone's schedule", {}))
        self.assertTrue(_looks_multistep("check each kid's homework and then remind them", {}))

    def test_looks_multistep_multiple_questions(self):
        from claude_service import _looks_multistep
        self.assertTrue(_looks_multistep("Is Child1 home? And what's for dinner?", {}))

    def test_looks_multistep_simple_lookup_is_false(self):
        from claude_service import _looks_multistep
        self.assertFalse(_looks_multistep("what's on the calendar today", {}))
        self.assertFalse(_looks_multistep("is anyone home?", {}))
        self.assertFalse(_looks_multistep("weather tomorrow", {}))

    def test_looks_multistep_respects_config_patterns(self):
        from claude_service import _looks_multistep
        cfg = {"executor": {"smol_intent_patterns": [r"\bmeal plan\b"]}}
        self.assertTrue(_looks_multistep("do the meal plan", cfg))
        # custom list replaces defaults -> 'compare' no longer triggers
        self.assertFalse(_looks_multistep("compare a and b", cfg))

    def _route(self, *, model, exec_cfg, user_message):
        import claude_service
        import config as config_mod
        from executor import ServiceRefs
        orig = config_mod.config
        try:
            config_mod.config = {"executor": exec_cfg}
            return claude_service._get_executor(
                "chat", ServiceRefs(), model=model, user_message=user_message,
            )
        finally:
            config_mod.config = orig

    def test_intent_routes_multistep_to_smol(self):
        from executors.smol import SmolExecutor
        ex = self._route(
            model="claude-sonnet-4-6",
            exec_cfg={"chat": "native", "chat_routing": "intent"},
            user_message="compare the two schedules and flag conflicts",
        )
        self.assertIsInstance(ex, SmolExecutor)

    def test_intent_keeps_simple_lookup_native(self):
        from executors.native import NativeToolExecutor
        ex = self._route(
            model="claude-sonnet-4-6",
            exec_cfg={"chat": "native", "chat_routing": "intent"},
            user_message="what's on the calendar today",
        )
        self.assertIsInstance(ex, NativeToolExecutor)

    def test_static_routing_ignores_message(self):
        from executors.native import NativeToolExecutor
        ex = self._route(
            model="claude-sonnet-4-6",
            exec_cfg={"chat": "native"},  # no chat_routing -> static default
            user_message="compare a and b",
        )
        self.assertIsInstance(ex, NativeToolExecutor)

    def test_model_override_beats_simple_lookup(self):
        from executors.smol import SmolExecutor
        ex = self._route(
            model="or-deepseek-v4",
            exec_cfg={"chat": "native", "chat_routing": "intent",
                      "smol_models": ["or-deepseek-v4"]},
            user_message="what's on the calendar today",
        )
        self.assertIsInstance(ex, SmolExecutor)


class TestLiveDataRouting(unittest.TestCase):
    """Live-data queries force native even when chat default is smol."""

    def _route(self, *, model, exec_cfg, user_message):
        import claude_service
        import config as config_mod
        from executor import ServiceRefs
        orig = config_mod.config
        try:
            config_mod.config = {"executor": exec_cfg}
            return claude_service._get_executor(
                "chat", ServiceRefs(), model=model, user_message=user_message,
            )
        finally:
            config_mod.config = orig

    def test_looks_live_data_detects_vehicle_lock(self):
        from claude_service import _looks_live_data
        self.assertTrue(_looks_live_data("lock FamilyCar", {}))
        self.assertTrue(_looks_live_data("what's the car battery level?", {}))

    def test_looks_live_data_respects_config_patterns(self):
        from claude_service import _looks_live_data
        cfg = {"executor": {"native_intent_patterns": [r"\bthermostat\b"]}}
        self.assertTrue(_looks_live_data("check the thermostat", cfg))
        self.assertFalse(_looks_live_data("lock FamilyCar", cfg))

    def test_live_data_routes_to_native_on_smol_surface(self):
        from executors.native import NativeToolExecutor
        ex = self._route(
            model="claude-sonnet-4-6",
            exec_cfg={"chat": "smol", "chat_routing": "intent"},
            user_message="lock FamilyCar",
        )
        self.assertIsInstance(ex, NativeToolExecutor)

    def test_live_data_beats_smol_model_override(self):
        from executors.native import NativeToolExecutor
        ex = self._route(
            model="or-deepseek-v4",
            exec_cfg={"chat": "smol", "chat_routing": "intent",
                      "smol_models": ["or-deepseek-v4"]},
            user_message="lock FamilyCar",
        )
        self.assertIsInstance(ex, NativeToolExecutor)

    def test_multistep_compare_without_wearables_still_routes_smol(self):
        from executors.smol import SmolExecutor
        ex = self._route(
            model="claude-sonnet-4-6",
            exec_cfg={"chat": "smol", "chat_routing": "intent"},
            user_message="compare tomorrow's schedule and the weather",
        )
        self.assertIsInstance(ex, SmolExecutor)

    def test_wearable_compare_routes_native_not_smol(self):
        from executors.native import NativeToolExecutor
        ex = self._route(
            model="or-deepseek-v4",
            exec_cfg={"chat": "smol", "chat_routing": "intent",
                      "smol_models": ["or-deepseek-v4"]},
            user_message="compare garmin and oura",
        )
        self.assertIsInstance(ex, NativeToolExecutor)


class TestShadowToolDomainParity(unittest.TestCase):
    """The shadow eval path must apply the same mode domain filter as the
    primary — otherwise the shadow runs with a bigger toolset (it could call
    get_oura_sleep when the mode-restricted primary can't), polluting the eval."""

    def _capture_smol_tools(self, tool_domains):
        from unittest.mock import patch
        import eval_service

        search_tools = [{
            "name": "web_search",
            "description": "search",
            "input_schema": {"type": "object", "properties": {}},
        }]
        all_tools = search_tools + [{
            "name": "get_oura_sleep",
            "description": "sleep",
            "input_schema": {"type": "object", "properties": {}},
        }]
        captured = {}

        async def fake_triplet(**kwargs):
            captured.update(kwargs)
            return None

        def fake_get_schemas(group, cal_available=False, domains=None):
            if domains == ["search"]:
                return list(search_tools)
            if domains is None:
                return list(all_tools)
            return []

        async def _run_capture():
            from eval.policy import resolve_eval_policy
            from llm.shadow_hooks import _fire_shadow_deferred

            policy = resolve_eval_policy({
                "eval": {"enabled": True, "shadow_model": "or-mimo-25-pro"},
                "executor": {"shadow_harness_enabled": True},
            })

            mock_gw = unittest.mock.MagicMock()
            mock_gw.get_tool_schemas.side_effect = fake_get_schemas
            with patch.object(eval_service, "fire_shadow_triplet", fake_triplet), \
                 patch("tool_gateway.get_tool_gateway", return_value=mock_gw):
                await _fire_shadow_deferred(
                    0,
                    policy=policy,
                    harness_on=True,
                    shed_on_backpressure=policy.shed_on_backpressure,
                    config={
                        "eval": {"enabled": True, "shadow_model": "or-mimo-25-pro"},
                        "executor": {"shadow_harness_enabled": True},
                    },
                    user_message="compare oura and garmin sleep",
                    system="sys",
                    messages=[{"role": "user", "content": "hi"}],
                    primary_response="primary resp",
                    channel_id="",
                    actor_id="",
                    cal_service=None,
                    db_module=None,
                    session=None,
                    tz=None,
                    model="claude-sonnet-4-6",
                    group="family",
                    triggered_by="discord",
                    tool_domains=tool_domains,
                )

        asyncio.run(_run_capture())
        return {t["name"] for t in captured.get("smol_tools", [])}

    def test_shadow_respects_restrictive_mode_domains(self):
        names = self._capture_smol_tools(["search"])
        self.assertTrue(names, "shadow should still get search-domain tools")
        self.assertNotIn("get_oura_sleep", names)  # home domain filtered out

    def test_shadow_unfiltered_when_no_mode(self):
        names = self._capture_smol_tools(None)
        self.assertIn("get_oura_sleep", names)  # full surface when mode is None


if __name__ == "__main__":
    unittest.main()
