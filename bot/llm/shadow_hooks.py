"""Shadow hook logic (Phase 4.4 Session 4).

Deferred post-reply shadow firing; harness gated by config.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from telemetry import fire_and_forget
from .hashing import hashable_system_prefix
from .runtime import get_container

log = logging.getLogger(__name__)

async def _fire_shadow_deferred(
    defer_s: float,
    *,
    policy: Any,
    harness_on: bool,
    shed_on_backpressure: bool,
    config: dict,
    user_message: str,
    system: str,
    messages: list[dict],
    primary_response: str,
    channel_id: str,
    actor_id: str,
    cal_service,
    db_module,
    session,
    tz,
    model: str | None,
    group: str | None,
    triggered_by: str,
    tool_domains: list[str] | None,
) -> None:
    if defer_s > 0:
        await asyncio.sleep(defer_s)
    try:
        from eval_service import fire_shadow_triplet
        from executors.smol import SmolExecutor
        from tool_gateway import get_tool_gateway
        from executor import ServiceRefs, ExecutorConfig

        gw = get_tool_gateway()
        container = get_container()

        smol_exec = None
        smol_tools: list[dict] = []
        shadow_exec_config = None

        if harness_on:
            _shadow_services = ServiceRefs(
                calendar=cal_service,
                ha=container.ha if container else None,
                db=db_module,
                session=session,
                orchestrator=container.notification_orchestrator if container else None,
                identity=None,
                tz=tz,
                llm_for=container.llm_for if container else None,
            )
            smol_exec = SmolExecutor(gateway=gw).with_services(_shadow_services)
            smol_tools = gw.get_tool_schemas(
                group or "family",
                cal_available=cal_service is not None,
                domains=tool_domains,
            )
            shadow_exec_config = ExecutorConfig(
                surface="chat",
                model=model or config.get("model", "unknown"),
                shadow=True,
                prompt_hash=hashlib.sha256(
                    (user_message + hashable_system_prefix(system)).encode()
                ).hexdigest()[:16],
                person_id=actor_id,
                group=group or "family",
                actor_id=actor_id,
                channel_id=str(channel_id) if channel_id else None,
                triggered_by=triggered_by,
            )

        if harness_on:
            await fire_shadow_triplet(
                user_message=user_message,
                system_prompt=system,
                history=messages[:-1] if messages else [],
                primary_response=primary_response,
                primary_model=model or config.get("model", "unknown"),
                shadow_model=policy.shadow_model,
                config=config,
                channel_id=str(channel_id) if channel_id else "",
                actor_id=str(actor_id) if actor_id else "",
                smol_executor=smol_exec,
                smol_messages=messages,
                smol_system=system,
                smol_tools=smol_tools,
                smol_exec_config=shadow_exec_config,
                session=session,
                db_module=db_module,
                shed_on_backpressure=shed_on_backpressure,
            )
        else:
            from eval_service import fire_shadow_call
            await fire_shadow_call(
                user_message=user_message,
                system_prompt=system,
                history=messages[:-1] if messages else [],
                primary_response=primary_response,
                shadow_model=policy.shadow_model,
                config=config,
                channel_id=str(channel_id) if channel_id else "",
                actor_id=str(actor_id) if actor_id else "",
                db_module=db_module,
                session=session,
                shed_on_backpressure=shed_on_backpressure,
            )
    except Exception as e:
        log.warning("maybe_fire_shadow deferred task failed: %s", e)


def maybe_fire_shadow(
    config: dict,
    user_message: str,
    system: str,
    messages: list[dict],
    primary_response: str,
    base_url: str | None,
    channel_id: str = "",
    actor_id: str = "",
    *,
    cal_service=None,
    db_module=None,
    session=None,
    tz=None,
    model: str | None = None,
    group: str | None = None,
    triggered_by: str = "discord",
    tool_domains: list[str] | None = None,
) -> None:
    """Launch deferred shadow capture if policy.capture_enabled (pair or triplet)."""
    if not config.get("eval", {}).get("shadow_model"):
        return

    from eval.policy import harness_active, resolve_eval_policy
    policy = resolve_eval_policy(config)

    if not policy.capture_enabled:
        return
    shadow_model = policy.shadow_model
    if not shadow_model:
        return
    primary_model = model or config.get("model", "unknown")
    if shadow_model == primary_model:
        return

    defer_s = float(policy.defer_s)
    harness_on = harness_active(policy)
    fire_and_forget(_fire_shadow_deferred(
        defer_s,
        policy=policy,
        harness_on=harness_on,
        shed_on_backpressure=policy.shed_on_backpressure,
        config=config,
        user_message=user_message,
        system=system,
        messages=messages,
        primary_response=primary_response,
        channel_id=channel_id,
        actor_id=actor_id,
        cal_service=cal_service,
        db_module=db_module,
        session=session,
        tz=tz,
        model=primary_model,
        group=group,
        triggered_by=triggered_by,
        tool_domains=tool_domains,
    ))
