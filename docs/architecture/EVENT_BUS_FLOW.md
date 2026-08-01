# Event Bus Flow (Actual Topology)

**Critical fact:** There is **no Redis Streams** (or NATS, RabbitMQ, ARQ) implementation in the codebase. All planning references (claude_upgrade_plan.md, implementation-notes-28.html, activity_plan.md) state "Redis migration deferred indefinitely" and "cognitive_tasks table is sufficient for current single-worker scale."

This document describes the **actual** implemented eventing, queues, fan-out, producers, and consumers.

All facts from source (main.py roles, worker.py, background_scheduler.py, supervisor.py, database.py, ha_service.py, frigate_listener.py, notification_router.py, cross_container.py, cognitive_handlers/*, api.py ConnectionManager, activity_log paths, etc.).

## Mermaid Diagram — Actual Flows

```mermaid
flowchart LR
    subgraph Ingress["Ingress Producers"]
        D1[Discord messages / slash<br/>bot.py on_message]
        D2[Web chat /api/chat<br/>api.py]
        F1[Frigate MQTT events<br/>frigate_listener_loop + aiomqtt]
        H1[HA WebSocket state changes<br/>ha_service start_websocket]
        S1[BTS scheduled triggers<br/>discord.ext.tasks.Loop]
        C1[Cognition enqueue<br/>research_bridge / workers]
    end

    subgraph Queues["Queue / State (SQLite)"]
        CT[(cognitive_tasks<br/>status=queued/active/done<br/>claim_next_task + run_at index)]
        UT[(unified_tasks + task_events + task_links<br/>kanban state machine)]
        AL[(activity_log)]
        NL[(notification_log)]
        PH[(pending_hitl)]
        TO[(task_outputs<br/>keyed by task_id + key e.g. thread:log)]
        SH[(shadow_calls)]
        PR[(presence_current / presence_log)]
        CH[(conversation_history)]
    end

    subgraph Consumers["Consumers / Workers"]
        CW[CognitiveWorker<br/>_poll every 10s<br/>claim_next_task → handler]
        WD[WatchdogWorker<br/>every 60s<br/>reclaim + dead_letter]
        BTS[BackgroundTaskScheduler<br/>+ TaskSupervisor<br/>registered loops]
        FR[Frigate listener<br/>dedup + conditional notify]
        PR2[Presence callbacks<br/>arrive/depart/friend]
        NR[NotificationRouter<br/>quiet_hours + prefs + flush]
        DIS[Delivery handlers<br/>discord_reply etc.]
    end

    subgraph Cross["Cross-Container / Fan-out"]
        IP1[bernie-discord:9000/internal/post<br/>+ /internal/hitl/notify<br/>X-Internal-Auth]
        WS1[api ConnectionManager<br/>WS broadcast to web clients]
        DISC[Discord channels / DMs]
        MAIL[email via Gmail]
    end

    D1 -->|history + intent| E[chat path]
    D2 -->|chat_general| E
    F1 -->|person/object alert| FR
    H1 -->|person.* state| PR2
    S1 -->|nightly_digest, reflection,<br/>consolidation, nudge, audit...| BTS
    C1 -->|INSERT cognitive_tasks| CT

    CT -->|claim| CW
    CW -->|get_handler(type)| HANDLERS[reflection / consolidation /<br/>research / study_guide /<br/>research_deliver / ...]
    HANDLERS -->|write| TO
    HANDLERS -->|post| IP1
    HANDLERS -->|notify| NR

    WD -->|UPDATE cognitive_tasks<br/>status / reclaim| CT

    BTS -->|periodic| various[nightly_digest_loop,<br/>ReflectionWorker.run etc.]
    various -->|enqueue or direct| CT
    various -->|DB write| AL
    various -->|notify| NR

    FR -->|if allowed| NR
    PR2 -->|log + broadcast| AL
    PR2 -->|broadcast| WS1

    E -->|tool side effects| TG[ToolGateway]
    TG -->|always| AL
    TG -->|notify| NR

    NR -->|Discord send or queue| DISC
    NR -->|email| MAIL
    NR -->|log| NL

    IP1 -->|auth check| DISC

    WS1 -->|presence.update, chat.typing, ...| WebClients[web dashboard WS]

    AL -->|watchman + audit| WM[Watchman / eval audit]
    WM -->|shadow| SH
    WM -->|post| IP1

    %% Legend
    classDef queue fill:#e6f3ff,stroke:#333
    class CT,UT,AL,NL,PH,TO,SH,PR,CH queue
```

## Producers (Facts)

| Producer | What it Emits | Target | Code |
|----------|---------------|--------|------|
| Discord client (bot.py) | User messages, reactions, slash | chat path + DB conversation_history | on_message, tree commands |
| FastAPI /api (api.py) | Web chat messages + thread ops | chat_general + DB | chat_endpoint, thread endpoints |
| Frigate MQTT + listener | Person/object events (with dedup) | conditional notification + snapshot | frigate_listener.py (aiomqtt), _seen_tracks + sent_reminders |
| HA WebSocket | state_changed for person.*, device_tracker.*, etc. | presence._on_person_state_change | ha_service (subscribe_events) |
| BackgroundTaskScheduler (registered loops) | Periodic ticks (10s/60s/time-of-day) | worker poll, nightly_digest, reflection, etc. | background_scheduler + supervisor |
| Cognitive handlers / research_bridge | Enqueue research / study / reflection | cognitive_tasks INSERT | worker_shared, research_bridge.py |
| ToolGateway (all executors) | Tool side effects + logs | activity_log + (future) Langfuse + notify | tool_gateway.py |
| NotificationRouter / delivery | Outbound messages | Discord, email, queue | notification_router.py |
| PresenceService callbacks | arrive/depart/friend | activity + WS broadcast | presence_service.py |
| Watchdog / reclaim | Task state transitions | cognitive_tasks + unified virtuals | worker + database |
| Eval / shadow / watchman | shadow_calls, audit events | DB + #anvil via post | eval/, watchman.py |
| Web WS clients | (implicit) typing / updates from server | (receive only) | api ConnectionManager |

## Consumers / Handlers

| Consumer | Poll / Trigger | Action | Owner Role |
|----------|----------------|--------|------------|
| CognitiveWorker | DB poll every 10s (`claim_next_task`) | dispatch HANDLERS[type], write task_outputs, post_to_discord | cognition |
| WatchdogWorker | DB poll every 60s | reset zombies, fail dead, project system cards | cognition |
| BTS loops | discord.ext.tasks (seconds/minutes/time) | nightly_digest_loop, ReflectionWorker, ConsolidationWorker, proactive_nudge, Watchman, etc. | cognition (most); some discord |
| Frigate listener | MQTT subscribe + loop | dedup, night/away gate, snapshot + notify | discord |
| HA WS listener | WS subscription | forward person state to presence | discord |
| Presence polling (residual) | 60s+ fallback | UniFi + HA adapter poll | discord |
| NotificationRouter flush | quiet end or /reminders on or batch threshold | drain queued high/normal | any (via router) |
| Internal post server | HTTP /internal/post + /hitl/notify | Discord send + HITL DMs | discord role only |
| WebSocket ConnectionManager | internal broadcast calls | push to connected web clients | api role |
| Eval / judges (nightly) | scheduled + DB scan | shadow_calls → judge_pair/triplet (PydanticAI) | cognition |

## Fan-out Mechanisms (Implemented)

- **DB polling** (cognitive_tasks): single-writer claim (status + run_at) → multiple handler types. No pub/sub.
- **BTS + Supervisor**: N registered loops fan out scheduled work; health + restart counts.
- **Callbacks**: presence on_arrive/on_depart, HA _on_person_state_change wired at startup.
- **Direct function calls** inside process (chat → gateway → handler).
- **Internal HTTP** (bernie-net only): cognition → discord for delivery + HITL notify. Auth via secret header. Fail-closed.
- **WS broadcast** (in-memory): api.py ConnectionManager to dashboard clients (presence, typing, etc.).
- **Discord channels + DMs**: primary user-visible fan-out.
- **Email**: secondary (Gmail).
- **activity_log / notification_log**: append-only audit consumed by watchman, dashboard, eval.
- **task_outputs + unified_tasks + task_events**: shared state for board + agent results (research threads use thread:log key).

## No-Op / Partial / Deferred

- **Redis Streams**: [not implemented]. All references are planning notes only. "cognitive_tasks table" is the queue.
- **Langfuse tool spans**: `langfuse_service.lf_tool_span` is explicit no-op stub. Generation logging path is active via observability.
- **MQTT as general bus**: Only Frigate consumer; broker details in config.mqtt but no other publishers in Bernie code.
- **HA event subscription**: Only state_changed for presence-relevant; full entity registry via REST poll on start.
- **Web push / external events**: None.
- **HITL resume**: Via pending_hitl table + internal notify + admin DM action. Not automatic retry bus.

## Container Split Impact (Wave 2b)

- Discord role owns: Discord client, HA WS start, presence start, frigate listener loop, internal post server (9000).
- API role owns: FastAPI serve + WS manager.
- Cognition role owns: BTS start, Cognitive/Watchdog registration + polling, nightly loops. Uses `post_to_discord` for output.
- Shared: bind-mounted SQLite (exact same file during shadow testing to measure contention).

All cross-role coordination uses either the shared DB or the authenticated internal HTTP endpoints.

## Database "Event" Tables (Actual)

- `cognitive_tasks`: primary async job queue.
- `unified_tasks` / `task_*`: kanban + execution audit.
- `activity_log`: everything Bernie does (tools, presence, notifications, audits).
- `notification_log`: delivery attempts.
- `pending_hitl`: tier-3 holds.
- `shadow_calls`, `family_insights`, `tomorrow_context`, `routines`, `task_outputs`: derived / worker artifacts.
- `presence_*`, `conversation_history`, `ha_devices`: state snapshots.

No stream offsets, consumer groups, or fan-out keys beyond table + status + indexes.

**End of event bus doc.** Only implemented mechanisms documented. Redis Streams entry would be false.
