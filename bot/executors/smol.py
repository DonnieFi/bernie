"""SmolExecutor — smolagents CodeAgent wrapped behind the Executor interface.

Tool calls route through ToolGateway, inheriting RBAC, validation, shadow
blocking, and Langfuse spans identically to NativeToolExecutor.

The CodeAgent generates Python code and executes it via LocalPythonInterpreter.
Each tool is exposed as a synchronous callable that trampolines into the main
event loop via run_coroutine_threadsafe — the whole agent runs in a thread pool
so the asyncio loop stays free.

Security: only tool-wrapper callables are injected into the interpreter.
The gateway and ServiceRefs are never reachable from inside the sandbox.
"""
from __future__ import annotations

import asyncio
import keyword
import logging
import os
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from executor import ExecutorConfig, ServiceRefs, ToolContext
from tool_gateway import ToolValidationError
import db_writes

log = logging.getLogger(__name__)

# Per-tool wall clock for the trampoline future. Tools that actually need
# longer (research, retrying weather/HA calls) should be split before this
# fires; 120s gives enough headroom for normal slow paths without leaking
# coroutines forever.
SMOL_TOOL_TIMEOUT_SECONDS = 120

# CodeAgent generates Python code blocks; 2048 truncates non-trivial plans.
SMOL_MODEL_MAX_TOKENS = 4096


# ── Tool factory ─────────────────────────────────────────────────────────────

def _make_gateway_tool(
    name: str,
    description: str,
    inputs_dict: dict,
    required_params: set[str],
    sync_fn,
):
    """Build a smolagents Tool whose forward() calls our sync_fn.

    required_params: set of parameter names that must not be nullable — derived
    from the JSON schema's "required" array. Required params get no default in
    forward() so the model's generated code must supply them.
    """
    from smolagents import Tool

    smol_inputs: dict = {}
    for k, v in inputs_dict.items():
        entry: dict = {
            "type": v.get("type", "string"),
            "description": v.get("description", ""),
        }
        if k not in required_params:
            entry["nullable"] = True
        smol_inputs[k] = entry

    params = list(smol_inputs.keys())
    # Guard exec() — every param name is interpolated into Python source, so
    # any non-identifier or keyword would silently produce a SyntaxError on
    # first call. Schema property names should already be snake_case but
    # nothing enforces that, so we assert here.
    bad = [p for p in params if not p.isidentifier() or keyword.iskeyword(p)]
    if bad:
        raise ValueError(
            f"_make_gateway_tool({name!r}): invalid parameter names {bad!r} — "
            f"each must be a valid Python identifier and not a keyword"
        )
    if params:
        # Python requires params without defaults to precede those with defaults.
        # Required params first (no default), then optional (=None).
        required_list = [p for p in params if p in required_params]
        optional_list = [p for p in params if p not in required_params]
        parts = required_list + [f"{p}=None" for p in optional_list]
        # Keep locals() lookup order consistent with smol_inputs key order.
        param_str = ", ".join(parts)
        fn_code = (
            f"def forward(self, {param_str}): "
            f"return sync_fn(**{{k: locals()[k] for k in {params!r}}})"
        )
    else:
        fn_code = "def forward(self): return sync_fn()"

    ns: dict[str, Any] = {"sync_fn": sync_fn}
    exec(fn_code, ns)  # noqa: S102

    attrs = {
        "name": name,
        "description": description,
        "inputs": smol_inputs if smol_inputs else {},
        "output_type": "string",
        "forward": ns["forward"],
        # Suppress smolagents' JSON-schema cross-check — we derive inputs from
        # the Anthropic schema, not from Python type hints.
        "skip_forward_signature_validation": True,
    }
    return type(name + "Tool", (Tool,), attrs)()


# ── Model adapter ─────────────────────────────────────────────────────────────

class _AnthropicSmolModel:
    """Thin synchronous wrapper so smolagents CodeAgent can call claude-* models.

    smolagents' CodeAgent calls model.generate() synchronously from whatever
    thread the agent is running in (we run it via run_in_executor). Using the
    Anthropic sync client avoids nested event-loop issues.
    """

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self._client = None  # lazy-initialised on first generate() call
        # Set by SmolExecutor._run_core before agent.run — used to acquire a
        # queue slot per messages.create (not one slot for the whole CodeAgent).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shadow: bool = False
        # Per-instance token accumulator. SmolExecutor.run reads + clears this
        # so callers (shadow eval, future cost dashboards) can see what the
        # CodeAgent's underlying LLM calls actually consumed.
        self.tokens_in_total = 0
        self.tokens_out_total = 0
        self.cache_creation_total = 0
        self.cache_read_total = 0

    def __call__(self, messages, stop_sequences=None, **kwargs):
        return self.generate(messages, stop_sequences=stop_sequences, **kwargs)

    def _queued_sync_create(self, create_kwargs: dict[str, Any]):
        """Acquire an LLM queue slot per API call (CodeAgent may call many times)."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return self._client.messages.create(**create_kwargs)

        from config import config as app_config

        exec_cfg = app_config.get("executor", {})
        timeout_s = float(exec_cfg.get("llm_step_timeout_s", 45))

        async def _run():
            from llm.queue import get_default_queue

            q = get_default_queue()
            from eval.policy import resolve_eval_policy
            policy = resolve_eval_policy(app_config)
            await q.configure(
                max_depth=int(exec_cfg.get("llm_queue_max_depth", 4)),
                shed_shadow_first=policy.shed_on_backpressure,
            )
            async with q.slot(shadow=bool(self._shadow)):
                return await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        None, lambda: self._client.messages.create(**create_kwargs)
                    ),
                    timeout=timeout_s,
                )

        future = asyncio.run_coroutine_threadsafe(_run(), loop)
        return future.result(timeout=timeout_s + 15)

    def generate(self, messages, stop_sequences=None, response_format=None, tools_to_call_from=None, **kwargs):
        from smolagents.models import ChatMessage, MessageRole
        if self._client is None:
            from anthropic import Anthropic
            self._client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

        # smolagents may pass tool-call / tool-response roles (ToolCallingAgent);
        # CodeAgent shouldn't, but normalise defensively.
        _ROLE_MAP = {
            "tool-call":     "assistant",
            "tool_call":     "assistant",
            "tool-response": "user",
            "tool_response": "user",
        }
        _VALID = {"user", "assistant"}

        # Collect all system-role messages and route them via Anthropic's
        # `system=` parameter. CodeAgent uses system-role messages for its
        # scaffolding (Python code-block format, final_answer contract,
        # examples); previously those were dropped, leading to weak harness
        # outputs in shadow eval. SmolExecutor.run also prepends Bernie's
        # system text to the user task, so the bot's system prompt flows in
        # both channels — Anthropic accepts them merged with a separator.
        system_parts: list[str] = []
        anthropic_messages = []
        for m in messages:
            if isinstance(m, dict):
                role = m.get("role", "user")
                content = m.get("content") or ""
            else:
                role = getattr(m.role, "value", str(m.role))
                content = m.content or ""
            if role == "system":
                if isinstance(content, str) and content.strip():
                    system_parts.append(content)
                continue
            role = _ROLE_MAP.get(role, role)
            if role not in _VALID:
                log.warning("_AnthropicSmolModel: unexpected role %r — mapping to 'user'", role)
                role = "user"
            anthropic_messages.append({"role": role, "content": content})

        if not anthropic_messages:
            return ChatMessage(role=MessageRole.ASSISTANT, content="Done!")

        create_kwargs: dict[str, Any] = {
            "model": self.model_id,
            "max_tokens": SMOL_MODEL_MAX_TOKENS,
            "messages": anthropic_messages,
        }
        if system_parts:
            create_kwargs["system"] = "\n\n".join(system_parts)
        if stop_sequences:
            create_kwargs["stop_sequences"] = stop_sequences

        # Let API errors propagate — swallowing them into the assistant
        # content makes CodeAgent treat a 429/401/network blip as a normal
        # turn (string "Error: ..."), preventing any retry/abort by the
        # framework and risking the error text being interpreted as code.
        # SmolExecutor.run() catches at the outer boundary.
        response = self._queued_sync_create(create_kwargs)
        # Accumulate token usage across every CodeAgent step so the harness leg
        # is billed visibly (was silently $0 in shadow_calls until now). Uses
        # the multi-provider extractor so non-Anthropic models (e.g. when the
        # harness is pinned to or-deepseek-v4) report their native cache fields.
        try:
            from executors.native import _extract_cache_tokens
            u = response.usage
            self.tokens_in_total += int(getattr(u, "input_tokens", 0) or 0)
            self.tokens_out_total += int(getattr(u, "output_tokens", 0) or 0)
            cc, cr = _extract_cache_tokens(u)
            self.cache_creation_total += int(cc or 0)
            self.cache_read_total += int(cr or 0)
        except Exception:
            pass
        text = "".join(
            block.text for block in response.content if hasattr(block, "text")
        ).strip()
        return ChatMessage(role=MessageRole.ASSISTANT, content=text)


# ── SmolExecutor ──────────────────────────────────────────────────────────────

class SmolExecutor:
    """smolagents CodeAgent behind the Executor Protocol.

    The agent runs in a thread (run_in_executor) so its synchronous model
    calls and tool trampolines don't block the event loop.
    """

    def __init__(self, gateway) -> None:
        self._gateway = gateway
        self._services: ServiceRefs = ServiceRefs()

    def with_services(self, services: ServiceRefs) -> "SmolExecutor":
        self._services = services
        return self

    def _make_ctx(self, config: ExecutorConfig) -> ToolContext:
        from config import config as app_config
        return ToolContext(
            config=app_config,
            person_id=config.person_id,
            group=config.group or "family",
            channel_id=config.channel_id,
            shadow=config.shadow,
            executor="smol",
            services=self._services,
            prompt_hash=config.prompt_hash,
            mode=config.mode,
            task_id=getattr(config, "task_id", None),
        )

    def _build_tool_wrappers(self, tools: list[dict], config: ExecutorConfig | None = None) -> list:
        """Build smolagents Tool objects from the caller-filtered tool schema list.

        Uses `tools` (already RBAC- and capability-filtered by get_tool_schemas)
        rather than the raw registry, so the model only sees tools the caller is
        authorised to use.
        """
        loop = asyncio.get_running_loop()

        wrappers = []
        for tool_schema in tools:
            name = tool_schema["name"]
            description = tool_schema["description"]
            input_schema = tool_schema.get("input_schema", {})
            props = input_schema.get("properties", {})
            required = set(input_schema.get("required", []))

            def _make_sync_fn(tool_name, exec_config):
                def sync_fn(**kwargs):
                    ctx = self._make_ctx(exec_config) if exec_config else None
                    future = asyncio.run_coroutine_threadsafe(
                        self._gateway.execute(tool_name, kwargs, ctx),
                        loop,
                    )
                    try:
                        return future.result(timeout=SMOL_TOOL_TIMEOUT_SECONDS)
                    except FutureTimeoutError:
                        # Schedule cancellation on the loop so the coroutine
                        # doesn't keep running (and burning Langfuse spans /
                        # network I/O) after we've moved on.
                        future.cancel()
                        log.warning(
                            "SmolExecutor tool %r exceeded %ds timeout — cancelled",
                            tool_name, SMOL_TOOL_TIMEOUT_SECONDS,
                        )
                        return (
                            f"Error in {tool_name}: timed out after "
                            f"{SMOL_TOOL_TIMEOUT_SECONDS}s"
                        )
                    except ToolValidationError as exc:
                        # CodeAgent self-corrects on traceback observations,
                        # so return the gateway's user-facing message verbatim.
                        return exc.message
                    except Exception as exc:
                        log.exception("SmolExecutor tool %r raised", tool_name)
                        return f"Error in {tool_name}: {exc}"
                return sync_fn

            sync_fn = _make_sync_fn(name, config)
            tool = _make_gateway_tool(name, description, props, required, sync_fn)
            wrappers.append(tool)

        return wrappers

    async def run(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        config: ExecutorConfig,
    ) -> str:
        """Protocol-conformant entry point. Returns text only."""
        text, _, _, _, _ = await self._run_core(messages, system, tools, config)
        return text

    async def _run_core(
        self,
        messages: list[dict],
        system: str | list[dict],
        tools: list[dict],
        config: ExecutorConfig,
    ) -> tuple[str, int, int, int, int]:
        """Run CodeAgent and return (text, tok_in, tok_out, cache_creation, cache_read).

        Callers that need token stats (e.g. _call_harness_shadow) use this
        directly so they never have to read mutable instance attributes that
        can be clobbered by a concurrent .run() call on the same instance.
        """
        from smolagents import CodeAgent

        tool_wrappers = self._build_tool_wrappers(tools, config)
        smol_model = _make_smol_model(config.model, self._services)
        # Per-call queue lives in _AnthropicSmolModel._queued_sync_create so each
        # CodeAgent step acquires/releases a slot; wrapping agent.run once would
        # hold the slot across tool execution and multiple LLM turns.
        if isinstance(smol_model, _AnthropicSmolModel):
            smol_model._loop = asyncio.get_running_loop()
            smol_model._shadow = bool(config.shadow)

        user_turns = [
            m.get("content", "") for m in messages
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        ]
        task = user_turns[-1] if user_turns else "Help the user."

        if isinstance(system, list):
            system_str = "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))
        else:
            system_str = system

        full_task = f"{system_str}\n\n---\n\n{task}" if system_str else task

        from config import config as app_config

        agent = CodeAgent(
            tools=tool_wrappers,
            model=smol_model,
            additional_authorized_imports=[],
            verbosity_level=int(app_config.get("executor", {}).get("smol_verbosity_level", 1)),
        )

        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: agent.run(full_task)
            )
            result_text = str(result) if result else "Done!"
            tok_in = getattr(smol_model, "tokens_in_total", 0) or 0
            tok_out = getattr(smol_model, "tokens_out_total", 0) or 0
            cc = getattr(smol_model, "cache_creation_total", 0) or 0
            cr = getattr(smol_model, "cache_read_total", 0) or 0
            await _log_smol_generation(
                config=config,
                services=self._services,
                user_input=full_task,
                output=result_text,
                input_tokens=tok_in,
                output_tokens=tok_out,
                cache_creation_tokens=cc,
                cache_read_tokens=cr,
            )
            return result_text, tok_in, tok_out, cc, cr
        except Exception as exc:
            log.exception("SmolExecutor: CodeAgent failed")
            err_text = f"Executor error: {exc}"
            tok_in = getattr(smol_model, "tokens_in_total", 0) or 0
            tok_out = getattr(smol_model, "tokens_out_total", 0) or 0
            await _log_smol_generation(
                config=config,
                services=self._services,
                user_input=full_task,
                output=err_text,
            )
            return err_text, tok_in, tok_out, 0, 0


async def _log_smol_generation(
    *,
    config: ExecutorConfig,
    services: ServiceRefs,
    user_input: str,
    output: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> None:
    """Fire a top-level Langfuse generation span for a SmolExecutor.run() turn,
    AND record the same usage to the local token_usage DB so dashboards see
    smol/harness cost. Per-tool spans still flow through ToolGateway._emit_span.
    """
    # 1. Local DB — this is the gap that hid harness_shadow cost until now.
    try:
        db = services.db if services and services.db else None
        if db is None:
            raise RuntimeError("no database module on ServiceRefs")
        await db_writes.routed("log_token_usage", 
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            model=config.model,
            conversation_id=config.conversation_id,
            triggered_by=(config.triggered_by or "discord") + ("/shadow" if config.shadow else ""),
            cache_creation_tokens=int(cache_creation_tokens or 0),
            cache_read_tokens=int(cache_read_tokens or 0),
            session_id=config.session_id,
            surface="shadow_harness" if config.shadow else "discord",
        )
    except Exception:
        log.debug("smol DB usage log failed (non-fatal)", exc_info=True)

    # 2. Langfuse
    try:
        from langfuse_logger import log_generation
        await log_generation(
            model=config.model,
            user_input=user_input,
            output=output,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            cache_creation_tokens=int(cache_creation_tokens or 0),
            cache_read_tokens=int(cache_read_tokens or 0),
            name="smol_chat",
            actor_id=config.person_id or "",
            triggered_by=config.triggered_by or "discord",
            session_id=config.session_id,
            metadata={
                "executor": "smol",
                "surface": config.surface,
                "group": config.group,
                "shadow": config.shadow,
                "mode": config.mode,
                **({"tools_advertised": config.tools_advertised} if config.tools_advertised is not None else {}),
                **({"tool_domain_count": config.tool_domain_count} if config.tool_domain_count is not None else {}),
            },
            tags=(
                ["smol", config.surface]
                + ([f"mode:{config.mode}"] if config.mode else [])
                + ([f"tools_advertised:{config.tools_advertised}"] if config.tools_advertised is not None else [])
                + ([f"tool_domains:{config.tool_domain_count}"] if config.tool_domain_count is not None else [])
            ),
        )
    except Exception:
        log.debug("smol langfuse trace failed (non-fatal)", exc_info=True)


def _make_smol_model(model_name: str, services: ServiceRefs):
    """Return a smolagents-compatible model for the given model name.

    Routing mirrors the rest of the bot:
    - claude-*                 → Anthropic direct (sync wrapper)
    - name in ollama_models    → Ollama direct via OpenAI-compat /v1 endpoint
                                 (full backup path; bypasses LiteLLM)
    - everything else          → LiteLLM proxy (or-*, etc.)
    """
    client_or_url = services.llm_for(model_name)

    if isinstance(client_or_url, str):
        # Ollama-direct: bypass LiteLLM entirely. Ollama exposes an OpenAI-
        # compatible /v1/chat/completions endpoint, so OpenAIModel with the
        # right api_base works without any LiteLLM dependency.
        try:
            from smolagents import OpenAIModel
        except ImportError as exc:
            raise RuntimeError(
                f"smolagents OpenAIModel unavailable for direct-Ollama route ({model_name}): {exc}"
            ) from exc
        return OpenAIModel(
            model_id=model_name,
            api_base=client_or_url.rstrip("/") + "/v1",
            api_key="ollama",  # placeholder — Ollama does not validate
        )

    # It's an AsyncAnthropic client. Check base_url for LiteLLM proxy vs Direct.
    base_url_str = str(client_or_url.base_url)
    if "api.anthropic.com" not in base_url_str:
        # Non-Claude, non-Ollama: route through the LiteLLM proxy as an
        # OpenAI-compat HTTP endpoint. The bot doesn't depend on the litellm
        # Python package anywhere else — production talks to the proxy via the
        # Anthropic SDK with `base_url=litellm_base_url`. Using OpenAIModel here
        # keeps that property (no new heavy dep) and uses the same library as
        # the Ollama-direct route.
        try:
            from smolagents import OpenAIModel
        except ImportError as exc:
            raise RuntimeError(
                f"smolagents OpenAIModel unavailable for LiteLLM route ({model_name}): {exc}"
            ) from exc
        api_base = base_url_str.rstrip("/")
        if not api_base.endswith("/v1"):
            api_base += "/v1"
        from model_registry import model_source
        from openrouter_models import resolve_openrouter_slug
        from config import config as _cfg

        model_id = model_name
        if model_source(model_name, _cfg) == "openrouter":
            model_id = resolve_openrouter_slug(model_name, _cfg)
        return OpenAIModel(
            model_id=model_id,
            api_base=api_base,
            api_key=client_or_url.api_key or "sk-openrouter",
        )
    
    # Direct Anthropic
    return _AnthropicSmolModel(model_id=model_name)
