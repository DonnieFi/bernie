from __future__ import annotations

import asyncio
import logging
import time

from executor import Executor, ExecutorConfig, ServiceRefs, ToolContext
from tool_gateway import ToolValidationError
from telemetry import fire_and_forget
import db_writes

log = logging.getLogger(__name__)


def tools_api_payload(schemas: list | None) -> dict:
    """Return kwargs for messages.create; omit tools key when surface is empty."""
    return {"tools": schemas} if schemas else {}


def _extract_cache_tokens(usage) -> tuple[int, int]:
    """Normalize cache token counts across provider response shapes.

    Returns (cache_creation_tokens, cache_read_tokens). Provider mapping:
      - Anthropic native: usage.cache_creation_input_tokens / cache_read_input_tokens
      - OpenAI / GPT (via LiteLLM): usage.prompt_tokens_details.cached_tokens (read only;
        provider doesn't expose write/miss separately)
      - DeepSeek (via LiteLLM raw): usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens
    Treats "miss" as a write-equivalent so the dashboard reflects upstream first-fill cost.
    """
    cc = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    if cc or cr:
        return cc, cr

    # DeepSeek native field names
    hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
    if hit or miss:
        return miss, hit  # miss → cache_creation, hit → cache_read

    # OpenAI / LiteLLM-normalized form: prompt_tokens_details.cached_tokens.
    # LiteLLM can emit `usage` itself as a plain dict on raw passthrough, and
    # the nested `prompt_tokens_details` likewise — handle both shapes.
    details = None
    if isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    else:
        details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens", 0) or 0)
        else:
            cached = int(getattr(details, "cached_tokens", 0) or 0)
        if cached:
            return 0, cached

    return 0, 0





async def _force_synthesis_turn(
    client, model, system, messages, note, max_tokens, *, app_config: dict, shadow: bool = False,
) -> str:
    """Make one tool-less turn that forces the model to answer from what it has
    already gathered. Returns the extracted text ('' on empty response).

    The loop's last message is a user/tool_result turn, so the note rides on
    THAT message rather than appending a second consecutive `user` turn (which
    Anthropic rejects with a 400). Omitting `tools` forces a textual answer
    instead of another tool call.
    """
    synthesis_messages = list(messages)
    if synthesis_messages and synthesis_messages[-1].get("role") == "user":
        last = dict(synthesis_messages[-1])
        note_block = {"type": "text", "text": note}
        content = last.get("content")
        if isinstance(content, list):
            last["content"] = content + [note_block]
        else:
            last["content"] = [{"type": "text", "text": str(content)}, note_block]
        synthesis_messages[-1] = last
    else:
        synthesis_messages.append({"role": "user", "content": note})
    from llm.queue import queued_messages_create

    final = await queued_messages_create(
        client,
        app_config,
        shadow=shadow,
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=synthesis_messages,
    )
    return " ".join(b.text for b in final.content if hasattr(b, "text")).strip()


class NativeToolExecutor:
    """Wraps the existing Anthropic tool-use loop behind the Executor interface.

    Dispatch goes through ToolGateway, never the legacy `_execute_tool` table.
    ServiceRefs are injected via `with_services(...)` before `run(...)`; tests
    that only check Protocol conformance can omit it.
    """

    def __init__(self, gateway) -> None:
        self._gateway = gateway
        self._services: ServiceRefs = ServiceRefs()

    def with_services(self, services: ServiceRefs) -> "NativeToolExecutor":
        self._services = services
        return self

    async def run(
        self,
        messages: list[dict],
        system: str | list[dict],
        tools: list[dict],
        config: ExecutorConfig,
    ) -> str:
        from config import config as app_config
        from llm.ollama import call_ollama
        from llm.observability import log_llm_turn

        client_or_url = self._services.llm_for(config.model)
        if isinstance(client_or_url, str):
            return await call_ollama(
                system, messages, app_config, None,
                model_override=config.model,
                session_id=config.session_id,
                conversation_id=config.conversation_id,
            )

        client = client_or_url
        _trace_user_input = next(
            (m.get("content", "") for m in reversed(messages)
             if m.get("role") == "user" and isinstance(m.get("content"), str)),
            "",
        )[:800]

        ctx = ToolContext(
            config=app_config,
            person_id=config.person_id,
            group=config.group or "family",
            channel_id=config.channel_id,
            shadow=config.shadow,
            executor="native",
            services=self._services,
            prompt_hash=config.prompt_hash,
            mode=config.mode,
            task_id=getattr(config, "task_id", None),
        )

        # NOTE: Do NOT add cache_control to the tools array. Cache prefixes are
        # computed left-to-right across system+tools+messages, and our system
        # contains a dynamic block (time/weather/presence) AFTER the cached
        # static block. A marker on tools forces Anthropic to cache the
        # dynamic block too — which invalidates every turn, costing +25% on
        # ~6500 tokens per turn for zero reuse. Keep caching system-only.

        _max_steps = int(app_config.get("executor", {}).get("max_steps", 5))
        _step_timeout_s = float(app_config.get("executor", {}).get("llm_step_timeout_s", 45))
        _max_tokens = int(app_config.get("executor", {}).get("max_tokens", 4096))
        _last_text = ""  # last non-empty visible text seen across the loop
        _validation_failures: dict[str, int] = {}
        _tools_called: set[str] = set()
        _prev_in = 0  # for delta computation per llm_iteration

        def _finish(result: str) -> str:
            if config.health_sleep_watch:
                from health_sleep import log_health_sleep_turn_tools
                log_health_sleep_turn_tools(
                    prefetch_ok=config.health_sleep_prefetch_ok,
                    model_tools_called=_tools_called,
                    channel_id=config.channel_id,
                )
            return result

        async def _create_messages(**kwargs):
            from llm.queue import queued_messages_create

            return await queued_messages_create(
                client,
                app_config,
                shadow=bool(config.shadow),
                **kwargs,
            )

        def _tools_payload(schemas: list | None) -> dict:
            return tools_api_payload(schemas)

        for _ in range(_max_steps):
            call_start = time.monotonic()
            try:
                response = await _create_messages(
                    model=config.model,
                    max_tokens=_max_tokens,
                    system=system,
                    messages=messages,
                    **_tools_payload(tools),
                )
            except asyncio.TimeoutError:
                log.warning(
                    "NativeToolExecutor: LLM step timed out after %ss model=%s",
                    _step_timeout_s, config.model,
                )
                return _finish(_last_text or (
                    "That took longer than expected — here's what I have so far. "
                    "Try asking again if you need more."
                ))
            except Exception as e:
                # Fallback: if caching is not supported by this model/provider,
                # strip cache_control and try again immediately.
                # Specifically catch Anthropic's BadRequestError or LiteLLM's 400 passthrough
                err_msg = str(e).lower()
                is_cache_error = "cache_control" in err_msg or "prompt caching" in err_msg or "not supported" in err_msg
                
                if is_cache_error:
                    log.warning(f"NativeToolExecutor: caching not supported for {config.model}, falling back...")
                    # Strip cache_control from system
                    fallback_system = system
                    if isinstance(system, list):
                        fallback_system = []
                        for block in system:
                            new_block = block.copy()
                            new_block.pop("cache_control", None)
                            fallback_system.append(new_block)
                    
                    # Strip cache_control from tools
                    fallback_tools = tools
                    if tools:
                        fallback_tools = []
                        for t in tools:
                            new_t = t.copy()
                            new_t.pop("cache_control", None)
                            fallback_tools.append(new_t)

                    # Retry once without caching
                    response = await _create_messages(
                        model=config.model,
                        max_tokens=1024,
                        system=fallback_system,
                        messages=messages,
                        **_tools_payload(fallback_tools),
                    )
                else:
                    raise e
            call_latency_ms = int((time.monotonic() - call_start) * 1000)
            try:
                from llm.turn_timer import TurnTimer

                _timer = TurnTimer.current()
                if _timer:
                    _timer.record("llm", _timer.phases.get("llm", 0) + call_latency_ms)
                    _timer.advance()
            except Exception:
                pass

            in_tok  = getattr(response.usage, "input_tokens", 0) or 0
            out_tok = getattr(response.usage, "output_tokens", 0) or 0
            cache_creation_tok, cache_read_tok = _extract_cache_tokens(response.usage)

            log.info("native_llm_step model=%s latency_ms=%s in=%s out=%s stop=%s shadow=%s",
                     config.model, call_latency_ms, in_tok, out_tok,
                     getattr(response, "stop_reason", None), bool(config.shadow))

            # Prompt-hash + delta for bloat detection (perf P0)
            _delta = max(0, int(in_tok) - int(_prev_in))
            _prev_in = int(in_tok)
            try:
                from db_binding import get_database
                from telemetry import fire_and_forget as _faf
                _faf(db_writes.routed("log_llm_iteration",
                    turn_id=config.turn_id or config.prompt_hash or config.session_id,
                    step=_ + 1,  # 1-based step
                    prompt_hash=config.prompt_hash,
                    tokens_in=int(in_tok),
                    delta_tokens=_delta,
                    model=config.model,
                    latency_ms=call_latency_ms,
                    stop_reason=getattr(response, "stop_reason", None),
                    channel_id=config.channel_id,
                    person_id=config.person_id,
                ))
            except Exception:
                pass  # instrumentation must never affect path

            _iter_text = " ".join(
                block.text for block in response.content
                if getattr(block, "type", "") == "text" and isinstance(getattr(block, "text", None), str)
            ).strip()
            if _iter_text:
                _last_text = _iter_text

            # Every iteration of the tool-use loop is a separate paid API call. Log
            # each one so token_usage reflects actual billing — not just the final
            # end_turn iteration. This used to under-report 4-5× on tool-heavy chats.
            if not config.shadow:
                _tool_names = [b.name for b in response.content if getattr(b, "type", "") == "tool_use"]
                _output_summary = _iter_text or (f"[tool_use: {', '.join(_tool_names)}]" if _tool_names else "")
                fire_and_forget(log_llm_turn(
                    model=config.model,
                    user_input=_trace_user_input,
                    output=_output_summary,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    actor_id=config.actor_id or "",
                    triggered_by=config.triggered_by,
                    session_id=config.session_id,
                    cache_creation_tokens=cache_creation_tok,
                    cache_read_tokens=cache_read_tok,
                    conversation_id=config.conversation_id,
                    latency_ms=call_latency_ms,
                    mode=config.mode,
                    surface="discord",
                    tools_advertised=config.tools_advertised,
                    tool_domain_count=config.tool_domain_count,
                ))
            else:
                # Shadow path: record under distinct surface for cost split visibility
                _tool_names = [b.name for b in response.content if getattr(b, "type", "") == "tool_use"]
                _output_summary = _iter_text or (f"[tool_use: {', '.join(_tool_names)}]" if _tool_names else "")
                fire_and_forget(log_llm_turn(
                    model=config.model,
                    user_input=_trace_user_input,
                    output=_output_summary,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    actor_id=config.actor_id or "",
                    triggered_by=config.triggered_by,
                    session_id=config.session_id,
                    cache_creation_tokens=cache_creation_tok,
                    cache_read_tokens=cache_read_tok,
                    conversation_id=config.conversation_id,
                    latency_ms=call_latency_ms,
                    mode=config.mode,
                    surface="shadow",
                    tools_advertised=config.tools_advertised,
                    tool_domain_count=config.tool_domain_count,
                ))

            if response.stop_reason == "end_turn":
                if _iter_text:
                    return _finish(_iter_text)
                # Degenerate finish: the model ended its turn with no visible
                # text — seen with reasoning models whose output went entirely to
                # a stripped reasoning channel. Force one
                # tool-less synthesis turn rather than emitting the canned "Done!".
                try:
                    recovered = await _force_synthesis_turn(
                        client, config.model, system, messages,
                        "Answer the user now using the information already gathered "
                        "above. Do not call any tools. If something is still unknown, "
                        "say so plainly.",
                        _max_tokens,
                        app_config=app_config,
                        shadow=bool(config.shadow),
                    )
                except Exception as e:
                    log.warning("NativeToolExecutor: synthesis after empty end_turn failed: %s", e)
                    recovered = ""
                return _finish(recovered or _last_text or (
                    "I gathered the details but couldn't compose a reply just then. "
                    "Mind asking again?"
                ))

            if response.stop_reason != "tool_use":
                if response.stop_reason == "max_tokens":
                    return _finish(_iter_text or _last_text or "Sorry, my response was too long.")
                return _finish(_iter_text or _last_text or "Sorry, I couldn't complete that.")

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                log.info("Tool call: %s %s", block.name, block.input)
                _tools_called.add(block.name)
                _tool_t0 = time.monotonic()
                try:
                    result = await self._gateway.execute(block.name, block.input, ctx)
                except ToolValidationError as exc:
                    _validation_failures[block.name] = _validation_failures.get(block.name, 0) + 1
                    if _validation_failures[block.name] >= 2:
                        reliable = (
                            app_config.get("primary_reliable_model")
                            or app_config.get("active_model")
                            or config.model
                        )
                        if config.model != reliable:
                            log.warning(
                                "Escalating from %s to %s after 2 validation failures on %s",
                                config.model, reliable, block.name,
                            )
                            config = ExecutorConfig(**{**config.__dict__, "model": reliable})
                            escalated = self._services.llm_for(reliable)
                            if not isinstance(escalated, str):
                                client = escalated
                    result = exc.message

                _tool_ms = int((time.monotonic() - _tool_t0) * 1000)
                try:
                    from llm.turn_timer import TurnTimer
                    _t = TurnTimer.current()
                    if _t:
                        prev = _t.phases.get("tools", 0)
                        _t.record("tools", prev + _tool_ms)
                        _t.advance()  # ensure outer llm delta doesn't swallow
                except Exception:
                    pass

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user",      "content": tool_results},
            ]

            # Step budget exhausted without an end_turn. Rather than dead-end with
        # "try again" (misleading — a retry re-runs the same chain into the same
        # wall), make one final tool-less turn so we answer with whatever was
        # already gathered (e.g. "Ollama on Deba is unreachable; here's what I
        # checked"). Omitting `tools` forces the model to respond, not call more.
        #
        # IMPORTANT: the loop's last message is a user/tool_result turn, so we
        # must NOT append a second `user` message (Anthropic rejects consecutive
        # same-role turns with a 400). Append the instruction as a text block to
        # that existing user message instead, preserving role alternation.
        _note = (
            "You've hit the tool-step limit. Answer now using only what you've "
            "already gathered above — do not request more tools. If something "
            "failed or is still unknown, say so plainly."
        )
        try:
            final_text = await _force_synthesis_turn(
                client, config.model, system, messages, _note, _max_tokens,
                app_config=app_config,
                shadow=bool(config.shadow),
            )
            if final_text:
                return _finish(final_text)
        except Exception as e:
            log.warning("NativeToolExecutor: synthesis turn after step cap failed: %s", e)
        return _finish(_last_text or "I ran out of steps trying to complete that. Please try again.")
