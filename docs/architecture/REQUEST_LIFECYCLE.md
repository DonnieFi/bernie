# Request Lifecycle (Discord / Web → Response)

**Source of truth:** bot/main.py, bot/bot.py, bot/claude_service.py (facade), bot/llm/* (pipeline, routing, intent, chat, clients, messages, observability), bot/executor.py, bot/executors/{native.py,smol.py}, bot/tool_gateway.py, tools/*, services, database public API, hitl/, notification_router.py, cross_container.py.

All paths are **fact-only**. Error/retry paths included. [partial] and [stub] marked.

## High-Level Flowchart (Mermaid)

```mermaid
flowchart TD
    A[External Input] -->|Discord message or slash| B[bot.py: on_message / tree.command]
    A -->|Web /api/chat or OpenWebUI shim| C[api.py: chat_endpoint → chat_general]
    A -->|Internal delivery| D[cross_container.post_to_discord or NotificationRouter]

    B --> E[claude_service.chat / chat_general / chat_meal_planning]
    C --> E
    D --> E

    E --> F[build_context + system prompt<br/>+ history from DB + memory.json + modes + routines/tomorrow_context]
    F --> G[llm/messages.py: prepare_messages<br/>prune tool results]

    G --> H{llm/routing.py: get_executor<br/>surface=chat or workers}
    H -->|executor_override or looks_live_data| I[NativeToolExecutor]
    H -->|smol_models or looks_multistep| J[SmolExecutor]
    H -->|default from executor.<surface>| K[Native or Smol per config]

    I --> L[Native loop: model.messages.create<br/>with tools]
    J --> M[Smol CodeAgent: generate + interpret<br/>via tool trampoline]

    L --> N{Tool call?}
    M --> N
    N -->|No / end_turn or empty| O[llm/observability.log_llm_turn<br/>+ langfuse_logger<br/>+ return synthesis]
    N -->|Yes| P[ToolGateway.execute]

    P --> Q[RBAC _role_allowed + group]
    Q -->|deny| R[return "permission denied" string]
    Q -->|ok| S[JSON schema validation]
    S -->|fail| T[ToolValidationError → executor retry logic]
    S -->|ok| U[hitl/hitl_service.check_tier]
    U -->|Tier 1| V[proceed]
    U -->|Tier 2| W[proceed + #anvil audit + activity_log hitl_tier2]
    U -->|Tier 3| X[write pending_hitl<br/>notify_hitl_pending via internal POST<br/>return held message<br/>await resume_pending with approved]
    V --> Y{shadow ctx? + is_write?}
    W --> Y
    Y -->|block| Z[return synthetic shadow block]
    Y -->|allow| AA[invoke registered fn via registry<br/>+ lf_tool_span fire-and-forget<br/>+ activity_log]
    AA --> AB[handler: e.g. ha_service, calendar, db public fn,<br/>weather, transit, snapshots, oura, etc.]

    AB --> AC[truncate per tool_gateway.max_result_chars / per-tool]
    AC --> AD[return tool result string/JSON]
    AD --> L

    T --> L
    X --> AE[admin DM approve/deny via hitl_discord<br/>→ resume with hitl_approved=True]
    AE --> AF[ToolGateway re-dispatch with flag]

    O --> AG[claude_service / llm/chat returns text]
    AG --> AH[bot.send to channel/DM or<br/>cross_container post or<br/>NotificationRouter.notify]
    AH --> AI[log notification + activity]

    subgraph Error Paths
        ER1[Anthropic/LiteLLM/Ollama exception] --> ER2[llm/ollama.py fallback or<br/>_call_ollama for chat failure]
        ER2 --> O
        ER3[HA/Frigate/Unifi/3rd party timeout] --> ER4[tool returns error string<br/>model sees it in next turn]
        ER4 --> N
        ER5[DB lock / contention] --> ER6[aiosqlite busy_timeout +<br/>_log_lock_error to activity<br/>retry at caller]
        ER6 --> P
        ER7[supervisor task crash >3] --> ER8[#anvil alert + restart policy]
    end

    style P fill:#f9f,stroke:#333
    style U fill:#ff9,stroke:#333
```

## Detailed Lifecycle (Narrative, Fact-Based)

1. **Ingress**
   - Discord: `bot.py` registers `on_message`, tree commands (`/summary`, `/weather`, `/bus *`, `/settings`, admin `/model` etc.). Message content + history fetched via `database.get_history`.
   - Web: `api.py` `/api/chat` (auth via web pin or token) calls `chat_general` directly. Also OpenAI shim path.
   - Slash commands may short-circuit (e.g. weather) or go to chat. NL parity rule documented in CLAUDE.md but not all commands have runtime tool parity.
   - Proactive / scheduled delivery: `NotificationRouter` or direct `post_to_discord` (cognition role).

2. **Context Assembly** (`claude_service` + `llm/chat.py` + `context.py`)
   - `build_system_prompt` / mode-specific (modes/*.md + docs/soul.md etc.).
   - Inject: presence, calendar slice, memory_context (memory.json), routines, tomorrow_context, family_insights, identity, health prefs.
   - History tail (verbatim recent + tool result pruning).
   - Mode resolution by channel or override.

3. **Routing Decision** (`llm/intent.py`, `llm/routing.py`)
   - `get_executor(surface, ..., user_message)`:
     1. explicit override (health_sleep prefetch forces native).
     2. `looks_live_data` (DEFAULT_NATIVE_INTENT_PATTERNS + delegated health_sleep) → native (chat only).
     3. model in `executor.smol_models` → smol.
     4. `chat_routing=="intent"` + `looks_multistep` (patterns or 2+ ? ) → smol.
     5. surface default (`executor.chat` / `workers` — workers noted as dead).
   - Intent detectors are pure regex + config; no model call.
   - Live data wins over smol_models.

4. **Executor Run** (`executors/native.py`, `executors/smol.py`, `executor.py`)
   - Native: classic Anthropic tool-use loop (max_steps from config). On tool call → gateway. On empty `end_turn` → `_force_synthesis_turn` (native recovers "Done!" cases).
   - Smol: smolagents CodeAgent in threadpool; each tool wrapped to trampoline via `run_coroutine_threadsafe` into gateway (120s wall per tool). Only safe callables exposed.
   - Both build `ExecutorConfig` (model, person_id, shadow, prompt_hash, mode, health_sleep_* flags, etc.).
   - `prompt_hash` computed for eval dedup/shadow storage.
   - Every completion: `log_llm_turn` (DB tokens + langfuse).

5. **Tool Execution Gate** (`tool_gateway.py`)
   - Unknown tool → error string.
   - RBAC: `_role_allowed(caller_group, role_required)`. Hierarchy: system > admin > parents > all. "kids" only all.
   - Schema: jsonschema validation → `ToolValidationError` (executor catches for retry/escalation).
   - Shadow: if `ctx.shadow` and `is_write` → synthetic block (defense in depth in handlers too).
   - Tier: `hitl_service.check_tier` (1=proceed silent, 2=proceed+audit, 3=hold pending_hitl + DMs via internal notify + resume).
   - Dispatch: `registry[name]["fn"](args, ctx)` (ctx carries services, person, etc.).
   - Side effects: `lf_tool_span` (fire-and-forget, currently no-op in langfuse_service), `db.log_activity`.
   - Result truncation: global + per-tool caps (tool_gateway config). 0 = unlimited.
   - **Never call handler directly** — all paths (chat, prefetch, workers, one-offs) go through gateway.

6. **Handler Execution**
   - Most delegate to public `database.*` or injected services (ha, calendar, weather, presence, frigate, transit, oura, network, identity, food, garbage, memory, snapshots, litellm_admin, etc.).
   - Snapshot tools (`get_vehicle_status`, `get_sleep_summary`) return `{summary, core, extras}` contract for exact numbers.
   - Kanban tools are task-bound (`ctx.task_id`).
   - Research bridge links unified research task → cognitive task.

7. **Response / Delivery**
   - Text returned to caller (chat functions) → bot.send or channel.send or DM.
   - Cognition results: handler writes task_outputs, then delivery handler uses `post_to_discord` (internal) or email or DM.
   - Notifications: `NotificationRouter` applies quiet_hours, dm_mode prefs (`person_preferences`), urgency, batch flush. Logs to notification_log.
   - Web clients receive WS broadcasts for presence/chat/typing.

8. **Observability**
   - Every LLM turn: `llm.observability.log_llm_turn` → `database.log_token_usage` + `langfuse_logger.log_generation`.
   - Tools: gateway activity_log + planned Langfuse spans.
   - Activity aggregator for dashboard credits/cost.

## Error + Retry Paths (Explicit)

- Schema fail → ToolValidationError bubbles to executor step; executor includes error in messages for model retry (up to max_steps).
- Network / upstream 5xx/timeout in service → handler returns error text (model sees "failed to fetch...").
- Chat model failure → llm/ollama fallback path (`llm_fallback` or first ollama model) for chat surface.
- Cognitive worker failure → `db.fail_cognitive_task` → watchdog may dead-letter.
- HITL tier 3: held until admin DM action (approve/deny/expire); resume re-enters gateway with flag.
- Shadow write attempt: blocked before dispatch.
- DB lock storms: busy_timeout + daemon conn + heartbeat files for Docker health.
- Supervisor: per-task restart count; >3 → alert + status failed.
- No calendar service → domain filtering in gateway/tool list.
- Partial: live-data native recovery on empty end_turn (not "Done!").

## Fallbacks & Overrides

- `executor_override` in prefetch (health_sleep).
- `llm_fallback` config for chat.
- `set_model` / `/model` mutates active; re-read on reload.
- Config `native_intent_patterns` / `smol_intent_patterns` replace defaults when non-empty.
- Role in container split gates some init (cognition registers workers/BTS; discord owns WS + listeners).

**End of lifecycle doc.** Diagrams use only paths present in source. No assumed Redis or third runtime.
