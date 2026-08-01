"""Executor routing (native vs smol) for Phase 4.4 Session 2.

Moved from claude_service._get_executor.
Preserves exact precedence and behavior.
Uses llm.intent for the looks_* detectors (no claude_service dep).
All client routing still goes through ServiceContainer.llm_for.
ToolGateway construction for the executor is unchanged.
"""

from __future__ import annotations

from typing import Any

from .intent import looks_live_data, looks_multistep


def get_executor(
    surface: str,
    services: Any,
    model: str | None = None,
    user_message: str | None = None,
    executor_override: str | None = None,
):
    """Return the executor for the given surface ('chat' only for chat path).

    NOTE (tombstone): executor.workers / surface="workers" is dead.
    Cognitive workers use direct model calls + polling of the cognitive_tasks
    table (see bot/worker.py CognitiveWorker._poll, db.claim_next_task,
    and bot/supervisor.py). The legacy executor["workers"] config key has been
    removed; this path always used the chat surface or direct worker_shared.

    The surface config is the cutover switch for *chat*: `native` preserves the
    Anthropic tool-use loop, `smol` routes through SmolExecutor.

    Routing precedence (must not regress):
    1. executor_override (e.g. health sleep prefetch forces "native")
    2. live-data intent (chat only): looks_live_data -> native (wins over smol_models and multistep)
    3. Per-model override: model in executor.smol_models -> smol
    4. chat_routing=="intent" and looks_multistep -> smol
    5. Otherwise the surface default (executor.chat)

    All config-driven — no hardcoded model lists or query rules.
    """
    from config import config as app_config
    from tool_gateway import get_tool_gateway

    gw = get_tool_gateway()
    exec_cfg = app_config.get("executor", {})
    # surface=="workers" no longer used; cognitive path is separate (cognitive_tasks polling)
    executor_name = executor_override or exec_cfg.get(surface, "native")
    smol_models = exec_cfg.get("smol_models", [])
    force_smol = bool(model and model in smol_models)
    intent_smol = (
        surface == "chat"
        and exec_cfg.get("chat_routing") == "intent"
        and looks_multistep(user_message, app_config)
    )
    force_native = (
        surface == "chat"
        and looks_live_data(user_message, app_config)
    )
    if executor_override:
        pass  # caller already chose native vs smol
    elif force_native:
        executor_name = "native"
    elif force_smol or intent_smol:
        executor_name = "smol"
    if executor_name == "smol":
        from executors.smol import SmolExecutor
        return SmolExecutor(gateway=gw).with_services(services)
    from executors.native import NativeToolExecutor
    return NativeToolExecutor(gateway=gw).with_services(services)
