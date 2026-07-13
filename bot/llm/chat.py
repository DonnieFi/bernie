"""Chat entrypoints (Phase 4.4 Session 4).

Moved from claude_service.py:
- chat_general
- chat
- chat_meal_planning
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

    tool_domains = resolve_tool_domains(
        channel_id=channel_id,
        config=config,
        mode_domains=mode_ceiling,
        user_message=user_message or "",
        history=history,
        apply_intent_router=apply_intent_router,
    )

    post_channel = resolve_tool_domains(
        channel_id=channel_id,
        config=config,
        mode_domains=mode_ceiling,
        apply_intent_router=False,
    )

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
    if person_id:
        from memory_service import get_memory_context
        memory_context = await get_memory_context(person_id)

    from datetime import datetime
    from modes import resolve_mode, load_all_modes, get_mode_override
    from notification_router import _is_quiet_hours

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

    if live_context_override is not None:
        live_context = live_context_override
    else:
        try:
            live_context = await build_context(
                config, cal_service, _session,
                user_message=user_message or "",
                channel_id=channel_id or "",
                is_dm=is_dm,
                mode=mode_slug_for_ctx,
            )
        except Exception:
            live_context = {}

    from context import BernieContext
    services = _build_service_refs(_container)
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
            return fallback_res + "\n\n*(Bernie fallback mode — local Ollama)*"
        except Exception as fallback_exc:
            log.exception(f"Ollama fallback also failed: {fallback_exc}")
            raise chat_exc


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


async def chat_meal_planning(
    user_message: str,
    history: list[dict],
    config: dict,
    person_name: str | None = None,
    group: str | None = None,
    actor_id: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    channel_id: str | None = None,
    **kwargs
) -> str:
    """Legacy meal entry — #furnace uses chat_general + chef mode (40B). Phase 28-2c: delete."""
    tz = ZoneInfo(config["timezone"])
    _container = get_container()
    cal_service = _container.calendar if _container else None
    _session = _container.session if _container else None

    from context import MealContext
    bernie_ctx = MealContext.build(config)
    system = bernie_ctx.render_blocks()
    messages = _prepare_messages(history, user_message, config=config)
    model_snapshot, base_url_snapshot = get_model_info()
    client = _make_client(base_url_snapshot)
    person_id = None
    try:
        from constants import registry as person_registry
        person_id = person_registry.resolve(person_name) if person_name else None
    except Exception:
        pass
    effective_actor = actor_id or person_id or "unknown"

    # Wave 1a: route meal/furnace through the resolver so chef mode ceiling applies.
    # This fixes the previous full ~61-tool leak on the #furnace path.
    # Resolve mode (channel pin will select "chef" for furnace); then use surface resolver.
    from modes import load_all_modes, resolve_mode
    load_all_modes()
    _meal_mode = resolve_mode(
        channel=channel_id,
        person_id=person_id,
        message_text=user_message or "",
    )

    from tool_gateway import get_tool_gateway
    from llm.tool_surface import resolve_tool_domains, get_tool_schemas_for_turn, deferral_system_block
    tool_domains = resolve_tool_domains(
        mode=_meal_mode,
        channel_id=channel_id,
        config=config,
        apply_intent_router=False,  # chef allowlist is already the right-sized surface
    )
    gw = get_tool_gateway()
    tools = get_tool_schemas_for_turn(
        gw,
        group or "family",
        tool_domains,
        config,
        cal_available=cal_service is not None,
    )

    # Wave 2: always surface the active chef surface for #furnace transparency (even though not "narrowed" by intent).
    from llm.tool_surface import active_surface_summary
    chef_slug = getattr(_meal_mode, "slug", "chef")
    system.append({
        "type": "text",
        "text": active_surface_summary(tool_domains, len(tools), mode_slug=chef_slug),
    })
    defer_block = deferral_system_block(config)
    if defer_block:
        system.append({"type": "text", "text": defer_block})

    # Observability: log the resolved chef surface (so activity_log has the count/domains for furnace turns).
    try:
        from telemetry import fire_and_forget as _faf
        from db_binding import get_database
        _faf(db_writes.routed("log_tool_surface",
            tool_count=len(tools),
            domains=tool_domains,
            narrowed=False,  # chef is the mode ceiling itself for this path
            channel_id=channel_id,
            person_id=person_id,
        ))
    except Exception:
        pass

    try:
        result = await _run_loop(
            client, model_snapshot, system, messages, config,
            cal_service, None, tz, _session, tools,
            user_message=user_message,
            group=group, actor_id=effective_actor,
            base_url=base_url_snapshot,
            session_id=session_id,
            conversation_id=conversation_id,
            surface="chat",
            person_id=person_id,
            channel_id=channel_id,
            mode=chef_slug,
            tools_advertised=len(tools),
            tool_domain_count=len(tool_domains) if tool_domains is not None else None,
        )
        # Fire shadow with the chef-resolved tool_domains so harness sees the same surface as production meal turn.
        maybe_fire_shadow(
            config, user_message, system, messages, result,
            base_url_snapshot, channel_id=channel_id or "", actor_id=effective_actor,
            cal_service=cal_service, db_module=get_db(), session=_session,
            tz=tz,
            model=model_snapshot, group=group, triggered_by="discord",
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
                is_dm=False,
                mode="chef",
            )
            return fallback_res + "\n\n*(Bernie fallback mode — local Ollama)*"
        except Exception as fallback_exc:
            log.exception(f"Ollama fallback also failed: {fallback_exc}")
            raise chat_exc
    finally:
        await _close_client(client)
