"""Chat entrypoints (Phase 4.4 Session 4).

Moved from claude_service.py:
- chat_general
- chat
- _apply_health_sleep_prefetch (now _apply... or internal)
Preserves all: modes, prompts, BernieContext, tool_domains, health_sleep prefetch + routing,
suppress_shadow/openwebui, Ollama fallback behavior, etc.
No changes to modes/*, context.py, tool schemas.
Uses llm.runtime, llm.context_builder, and llm.shadow_hooks directly (no claude_service import).
"""

import logging
from zoneinfo import ZoneInfo

from telemetry import fire_and_forget  # if needed, but not direct here

log = logging.getLogger(__name__)

from .model_state import get_model_info, _base_url_for_model
from .clients import make_client as _make_client, close_client as _close_client
from .messages import prepare_messages as _prepare_messages
from .pipeline import run_loop as _run_loop
from .services import build_service_refs as _build_service_refs
from .runtime import get_container, get_db
from .context_builder import build_context
from .shadow_hooks import maybe_fire_shadow
import db_writes


def _resolve_turn_tools(
    *,
    config: dict,
    bernie_ctx,
    user_message: str,
    history: list[dict],
    group: str | None,
    cal_service,
    channel_id: str | None,
    is_dm: bool,
    live_context: dict,
    system: list,
    include_task_system: bool = False,
    apply_intent_router: bool = True,
) -> tuple[list[dict], list[str] | None]:
    """Intent router + slag funnel blocks + mode-aware tool schemas."""
    from tool_gateway import get_tool_gateway
    from llm.slag_funnel import should_suggest_slag, slag_funnel_system_block

    mode_domains = bernie_ctx.allowed_domains if getattr(bernie_ctx, "mode", None) else None

    if live_context.get("calendar_lazy"):
        system.append({
            "type": "text",
            "text": (
                "Calendar is not preloaded in this turn. Before saying the day is clear "
                "or quoting schedule times, call get_todays_events or get_week_events."
            ),
        })

    if should_suggest_slag(
        user_message or "",
        config=config,
        channel_id=channel_id,
    ):
        system.append({"type": "text", "text": slag_funnel_system_block(config)})

    # Use the single resolver (tool_surface) for mode ceiling (deny applied) + channel map + narrow.
    # This ensures channel_tool_domains (e.g. future conservative #slag) are respected before intent narrow,
    # and narrowed detection uses post-channel ceiling per plan.
    from llm.tool_surface import (
        resolve_tool_domains,
        get_tool_schemas_for_turn,
        append_tool_surface_ux,
    )

    mode_ceiling = mode_domains

    # family-bot-rpg: single channel-ceiling resolve; intent narrow is a pure step
    post_channel = resolve_tool_domains(
        channel_id=channel_id,
        config=config,
        mode_domains=mode_ceiling,
        apply_intent_router=False,
    )
    if apply_intent_router:
        from llm.intent_router import narrow_tool_domains as _narrow
        tool_domains = _narrow(
            mode_domains=post_channel,
            user_message=user_message or "",
            config=config,
            history=history,
            channel_id=channel_id,
        )
    else:
        tool_domains = post_channel

    gw = get_tool_gateway()
    if tool_domains is not None or mode_ceiling is not None:
        tools = get_tool_schemas_for_turn(
            gw,
            group or "family",
            tool_domains,
            config,
            cal_available=cal_service is not None,
        )
        mode_slug = getattr(getattr(bernie_ctx, "mode", None), "slug", None)
        narrowed = append_tool_surface_ux(
            system,
            config,
            tool_domains=tool_domains,
            tool_count=len(tools),
            mode_slug=mode_slug,
            mode_ceiling=mode_ceiling,
            post_channel_ceiling=post_channel,
        )
        try:
            from telemetry import fire_and_forget as _faf
            from db_binding import get_database
            from llm.turn_timer import TurnTimer
            _timer = TurnTimer.current()
            _faf(db_writes.routed("log_tool_surface",
                turn_id=getattr(_timer, "turn_id", None) if _timer else None,
                tool_count=len(tools),
                domains=tool_domains,
                narrowed=narrowed,
                channel_id=channel_id,
                person_id=getattr(bernie_ctx, "person_id", None),
            ))
        except Exception:
            pass
    else:
        # full surface (no mode ceiling)
        tools = get_tool_schemas_for_turn(
            gw, group or "family", None, config, cal_available=cal_service is not None,
        )

    if include_task_system:
        system.append({
            "type": "text",
            "text": (
                "Task System: LIVE. You have full access to household tasks and "
                "Kanban board tools. Use them proactively."
            ),
        })
    return tools, tool_domains


async def _apply_health_sleep_prefetch(
    *,
    user_message: str,
    config: dict,
    services,
    person_id: str | None,
    group: str | None,
    channel_id: str | None,
    system: list,
) -> tuple[str | None, bool, bool]:
    """Prefetch Garmin+Oura for sleep queries; return executor override + watch flags."""
    from health_sleep import (
        looks_health_sleep_query,
        prefetch_health_sleep,
        record_health_sleep_prefetch,
    )

    if not looks_health_sleep_query(user_message or "", config):
        return None, False, False

    health_prefetch = await prefetch_health_sleep(
        config=config,
        services=services,
        person_id=person_id,
        group=group or "family",
        channel_id=channel_id,
    )
    # db_module via lazy claude for compat
    db_mod = None
    try:
        db_mod = get_db()
    except Exception:
        pass
    await record_health_sleep_prefetch(
        health_prefetch,
        db_module=db_mod,
        person_id=person_id,
        channel_id=channel_id,
        user_message=user_message or "",
    )
    if health_prefetch.block:
        system.append({"type": "text", "text": health_prefetch.block})
    return (
        "native" if health_prefetch.block else None,
        True,
        bool(health_prefetch.ok),
    )


async def chat_general(
    user_message: str,
    history: list[dict],
    config: dict,
    person_name: str | None = None,
    triggered_by: str = "discord",
    model: str | None = None,
    group: str | None = None,
    actor_id: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    channel_id: str | None = None,
    openwebui: bool = False,
    suppress_shadow: bool = False,
    live_context_override: dict | None = None,
    is_dm: bool = False,
    **kwargs
) -> str:
    tz = ZoneInfo(config["timezone"])
    _container = get_container()
    cal_service = _container.calendar if _container else None
    _session = _container.session if _container else None

    memory_context = ""
    person_id = None
    try:
        from constants import registry as person_registry
        person_id = person_registry.resolve(person_name) if person_name else None
    except Exception:
        pass

    from datetime import datetime
    from modes import resolve_mode, load_all_modes, get_mode_override
    from notification_router import _is_quiet_hours
    import asyncio as _aio

    # Mode resolve is pure CPU — do before concurrent I/O so build_context gets mode
    load_all_modes()
    _resolved_mode = resolve_mode(
        channel=channel_id,
        person_id=person_id,
        message_text=user_message or "",
        quiet_hours_active=_is_quiet_hours(datetime.now(tz)),
        explicit_override=get_mode_override() or kwargs.get("mode"),
        openwebui=openwebui,
    )
    mode_slug_for_ctx = _resolved_mode.slug if _resolved_mode else (kwargs.get("mode") or "concierge")

    # family-bot-cib: overlap memory + live context (independent I/O legs)
    async def _fetch_memory() -> str:
        if not person_id:
            return ""
        try:
            from memory_service import get_memory_context
            return await get_memory_context(person_id)
        except Exception:
            return ""

    async def _fetch_live() -> dict:
        if live_context_override is not None:
            return live_context_override
        try:
            return await build_context(
                config, cal_service, _session,
                user_message=user_message or "",
                channel_id=channel_id or "",
                is_dm=is_dm,
                mode=mode_slug_for_ctx,
            )
        except Exception:
            return {}

    memory_context, live_context = await _aio.gather(_fetch_memory(), _fetch_live())

    from context import BernieContext
    services = _build_service_refs(_container)

    # family-bot-otc.1: start health prefetch early (overlaps BernieContext.build)
    from health_sleep import looks_health_sleep_query, prefetch_health_sleep
    _health_task = None
    if looks_health_sleep_query(user_message or "", config):
        _health_task = _aio.create_task(prefetch_health_sleep(
            config=config,
            services=services,
            person_id=person_id,
            group=group or "family",
            channel_id=channel_id,
        ))

    bernie_ctx = await BernieContext.build(
        config=config,
        person_id=person_id,
        channel_id=channel_id,
        tz=tz,
        services=services,
        is_dm=is_dm,
        memory_context=memory_context,
        live_context=live_context,
        openwebui=openwebui,
        user_message=user_message or "",
        mode=_resolved_mode,  # family-bot-2wh.2: avoid double resolve
    )
    system = bernie_ctx.render_blocks()

    # Phase 28 Wave 2c: Mode tagging for Langfuse
    mode_slug = bernie_ctx.mode.slug if bernie_ctx.mode else "concierge"

    messages = _prepare_messages(history, user_message, config=config)
    _tools, tool_domains = _resolve_turn_tools(
        config=config,
        bernie_ctx=bernie_ctx,
        user_message=user_message or "",
        history=history,
        group=group,
        cal_service=cal_service,
        channel_id=channel_id,
        is_dm=is_dm,
        live_context=live_context,
        system=system,
        include_task_system=True,
        apply_intent_router=not openwebui,
    )

    active_m, active_base = get_model_info()
    model_snapshot = model or active_m
    base_url_snapshot = active_base if model is None else _base_url_for_model(model_snapshot)
    effective_actor = actor_id or (person_id or "unknown")  # simplified

    executor_override: str | None = None
    health_sleep_watch = False
    health_sleep_prefetch_ok = False

    if _health_task is not None:
        from health_sleep import record_health_sleep_prefetch
        try:
            health_status = await _health_task
        except Exception:
            log.exception("early health_sleep prefetch failed")
            health_status = None
        if health_status is not None:
            db_mod = None
            try:
                db_mod = get_db()
            except Exception:
                pass
            await record_health_sleep_prefetch(
                health_status,
                db_module=db_mod,
                person_id=person_id,
                channel_id=channel_id,
                user_message=user_message or "",
            )
            if health_status.block:
                system.append({"type": "text", "text": health_status.block})
            executor_override = "native" if health_status.block else None
            health_sleep_prefetch_ok = bool(health_status.ok)
        else:
            health_sleep_prefetch_ok = False
        health_sleep_watch = True
        if health_sleep_prefetch_ok and _tools:
            _tools = [
                t for t in _tools
                if (t.get("name") if isinstance(t, dict) else None)
                not in {"get_sleep_summary", "get_oura_sleep"}
            ]

    # Subscription path (Codex/Grok via complete_subscription_chain + ToolGateway).
    subscription_ran = False
    try:
        from model_catalog import is_subscription_enabled
        from completion_router import subscription_model, CompletionRequest
        from subscription_complete import (
            complete_subscription_chain,
            complete_subscription_with_tools,
            log_subscription_attempts,
        )
        sub_entry = subscription_model(model_snapshot, config)
        if sub_entry is not None and is_subscription_enabled(model_snapshot, config):
            subscription_ran = True
            system_text = ""
            if isinstance(system, list):
                system_text = "\n\n".join(
                    b.get("text", "") if isinstance(b, dict) else str(b) for b in system
                )
            elif isinstance(system, str):
                system_text = system
            # Codex app-server accepts the canonical ToolGateway surface used by
            # OpenRouter. Keep the legacy extra narrowing only for Grok CLI.
            sub_tools = list(_tools or ())
            if (
                sub_entry.provider == "grok"
                and tool_domains is not None
                and len(sub_tools) > 10
            ):
                from llm.intent_router import narrow_tool_domains_for_subscription as _sub_narrow
                from llm.tool_surface import get_tool_schemas_for_turn as _sub_schemas
                from tool_gateway import get_tool_gateway as _sub_gw

                narrowed = _sub_narrow(
                    mode_domains=tool_domains,
                    user_message=user_message or "",
                    config=config,
                    history=history,
                )
                if narrowed is not None and len(narrowed) < len(tool_domains or []):
                    sub_tools = _sub_schemas(
                        _sub_gw(),
                        group,
                        narrowed,
                        config,
                        cal_available=cal_service is not None,
                    )
                    log.info(
                        "subscription intent narrow: %s -> %s tools (domains %s -> %s)",
                        len(_tools or ()),
                        len(sub_tools),
                        len(tool_domains or []),
                        len(narrowed),
                    )
            tool_schemas = tuple(sub_tools)
            # Web/OpenWebUI budget is shorter than Discord; leave headroom for hop overhead.
            chain_timeout = 160 if (openwebui or (triggered_by or "") == "web") else 180
            req = CompletionRequest(
                surface="chat",
                provider=sub_entry.provider,
                model=model_snapshot,
                messages=tuple(messages or ()),
                system=system_text or None,
                prompt=user_message or None,
                tools=tool_schemas,
                timeout_s=chain_timeout,
            )

            async def _exec_tool(name: str, args: dict) -> str:
                from tool_gateway import get_tool_gateway, ToolValidationError
                from executor import ToolContext
                gw = get_tool_gateway()
                ctx = ToolContext(
                    config=config,
                    person_id=person_id,
                    group=group or "family",
                    channel_id=channel_id,
                    shadow=False,
                    executor=sub_entry.provider,
                    services=services,
                )
                try:
                    out = await gw.execute(name, args or {}, ctx)
                except ToolValidationError as exc:
                    return exc.message
                return out if isinstance(out, str) else str(out)

            if tool_schemas:
                result, attempts = await complete_subscription_with_tools(
                    req, config, execute_tool=_exec_tool, max_steps=5
                )
            else:
                result, attempts = await complete_subscription_chain(req, config)
            log.info("subscription chat attempts=%s", attempts)
            try:
                await log_subscription_attempts(
                    attempts,
                    user_input=user_message or "",
                    final_text=result.text or "",
                    session_id=session_id,
                    conversation_id=conversation_id,
                    actor_id=effective_actor,
                    triggered_by=triggered_by or "discord",
                    surface="chat",
                )
            except Exception:
                log.exception("subscription attempt telemetry failed")
            if result.error is None and result.text:
                if not openwebui and not suppress_shadow:
                    try:
                        db_mod = None
                        try:
                            db_mod = get_db()
                        except Exception:
                            pass
                        maybe_fire_shadow(
                            config, user_message or "", system, messages, result.text,
                            base_url_snapshot,
                            channel_id=channel_id or "", actor_id=effective_actor,
                            cal_service=cal_service, db_module=db_mod, session=_session,
                            tz=tz,
                            model=model_snapshot, group=group, triggered_by=triggered_by,
                            tool_domains=tool_domains,
                        )
                    except Exception:
                        log.exception("subscription shadow hook failed")
                return result.text
            if tool_schemas:
                log.warning(
                    "subscription tool loop failed primary=%s attempts=%s; "
                    "not falling through to native/Ollama",
                    model_snapshot,
                    attempts,
                )
                # Stay fail-closed — subscription models cannot use llm_for / Ollama path.
            else:
                log.warning(
                    "subscription chain failed primary=%s attempts=%s",
                    model_snapshot,
                    attempts,
                )
    except Exception:
        log.exception("subscription chain path failed")

    if subscription_ran:
        return (
            "I couldn't finish that just now — try asking again in a moment."
        )

    if _health_task is None:
        executor_override, health_sleep_watch, health_sleep_prefetch_ok = (
            await _apply_health_sleep_prefetch(
                user_message=user_message or "",
                config=config,
                services=services,
                person_id=person_id,
                group=group or "family",
                channel_id=channel_id,
                system=system,
            )
        )
        if health_sleep_prefetch_ok and _tools:
            _tools = [
                t for t in _tools
                if (t.get("name") if isinstance(t, dict) else None)
                not in {"get_sleep_summary", "get_oura_sleep"}
            ]

    try:
        result = await _run_loop(
            None, model_snapshot, system, messages, config,
            cal_service, None, tz, _session, _tools,  # db will be set inside if needed
            user_message=user_message,
            triggered_by=triggered_by,
            group=group,
            actor_id=effective_actor,
            base_url=base_url_snapshot,
            session_id=session_id,
            conversation_id=conversation_id,
            is_dm=is_dm,
            person_id=person_id,
            channel_id=channel_id,
            services=services,
            mode=mode_slug,
            executor_override=executor_override,
            health_sleep_watch=health_sleep_watch,
            health_sleep_prefetch_ok=health_sleep_prefetch_ok,
            tools_advertised=len(_tools),
            tool_domain_count=len(tool_domains) if tool_domains is not None else None,
        )
        # Skip shadow eval for the OpenWebUI surface and for internal/meta
        # callers (e.g. session-title generation) — they aren't real family
        # turns and would otherwise burn shadow budget and pollute eval pairs.
        if not openwebui and not suppress_shadow:
            maybe_fire_shadow(
                config, user_message, system, messages, result,
                base_url_snapshot or _base_url_for_model(model_snapshot),
                channel_id=channel_id or "", actor_id=effective_actor,
                cal_service=cal_service, db_module=get_db(), session=_session,
                tz=tz,
                model=model_snapshot, group=group, triggered_by=triggered_by,
                tool_domains=tool_domains,
            )
        return result
    except Exception as chat_exc:
        log.exception(f"Primary model {model_snapshot} failed; attempting global Ollama fallback...")
        try:
            from .ollama import call_ollama as _call_ollama
            fallback_res = await _call_ollama(
                system=system,
                messages=messages,
                config=config,
                session=_session,
                cal_service=cal_service,
                session_id=session_id,
                conversation_id=conversation_id,
                user_message=user_message or "",
                channel_id=channel_id or "",
                is_dm=is_dm,
                mode=mode_slug,
            )
            # family-bot-ot4: no second full context rebuild — reuse already-built system/messages
            return fallback_res + "\n\n*(Bernie fallback mode — local Ollama)*"
        except Exception as fallback_exc:
            log.exception(f"Ollama fallback also failed: {fallback_exc}")
            # family-bot-otc.2 / ot4: calm family text — never re-raise stack to Discord
            return (
                "I couldn't finish that just now — try asking again in a moment."
            )


async def chat(
    user_message: str,
    history: list[dict],
    config: dict,
    person_name: str | None = None,
    is_dm: bool = False,
    group: str | None = None,
    actor_id: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    channel_id: str | None = None,
    **kwargs
) -> str:
    """Discord chat entry — thin wrapper over chat_general (2wh.15)."""
    log.info(f"chat called: is_dm={is_dm}")
    return await chat_general(
        user_message,
        history,
        config,
        person_name=person_name,
        triggered_by="discord",
        group=group,
        actor_id=actor_id,
        session_id=session_id,
        conversation_id=conversation_id,
        channel_id=channel_id,
        is_dm=is_dm,
        openwebui=False,
        **kwargs,
    )
