# DECISIONS (Architecture Decisions)

This file collects short ADRs for cross-cutting runtime and infra choices.
Full historical ADRs live in `docs/adr/`.

---

## ADR: SQLite WAL shared via bind-mount

**Status:** Accepted (ongoing since Phase 28 container split)

### Context
- Bernie runs three containers (discord, api, cognition) that all need access to the same state (tasks, history, activity, identity, routines, ...).
- Postgres or external DB was considered but rejected for household scale and ops simplicity.
- All containers bind-mount the host `./data` directory to `/data` inside the container (see docker-compose.yml).
- SQLite is opened in WAL mode (family_bot.db + -wal + -shm files) to allow concurrent readers while a writer is active.
- Public DB functions in `database.py` (and `db_binding`) are the only allowed access points; no raw `sqlite3.connect` / `sqlite_async.connect` or `_db_conn` in tools.

### Decision
Use a single SQLite file (WAL) shared by bind-mount across containers. One logical writer (cognition role primary for mutations; ADR-0003) with readers in other roles. Sync **stdlib sqlite3** via `sqlite_async` (`asyncio.to_thread` + write lock) with `busy_timeout` and WAL maintenance (40B-2A).

### Rationale
- Zero extra services/containers (no Postgres, no volume driver complexity).
- WAL gives good read concurrency for the dashboard/API while cognition writes.
- Sufficient for 1-5 person household load (few hundred events/day).

### Known Risks
- Bind-mount file locking semantics are host-FS dependent. On NFS, some overlay/network FS, or certain Docker Desktop setups, locks can fail or WAL checkpoint can corrupt.
- Simultaneous writers from multiple containers (if code regresses) will produce busy/locked errors or corruption.
- WAL journal files must stay co-located with the main .db (they do via the same mount).
- Crash of a writer mid-transaction can leave -wal that requires manual recovery in extreme cases.

### Migration Trigger Condition
Revisit / migrate when:
- Lock contention produces user-visible delays (>1s on routine reads) or dropped notifications in production logs.
- We need true multi-writer (e.g. separate cognition fleet or API accepting direct writes).
- We outgrow single-file scale (tens of GB or sustained >100 writes/sec).
- Host FS change makes reliable cross-process WAL impossible (e.g. move to named volumes + sidecar or Postgres).

Current mitigation: public DB API only, activity_log for diagnostics, watchdog + busy handling in worker.

---

## ADR: Redis Streams deferred

**Status:** Deferred indefinitely (no implementation)

### Context
Early planning (claude_upgrade_plan.md, implementation-notes-28, phase design docs) evaluated Redis Streams / NATS / ARQ / Rabbit for:
- reliable task queue with consumer groups
- activity feed fan-out
- event bus between roles

After container split experiment, the decision was to stay with DB-based queue.

### Decision
**No** Redis (or other external message broker) at this time.

Current substitute:
- `cognitive_tasks` table (with type, payload, status, claimed_at, result, stats).
- `db.claim_next_task()` + status transitions (claimed / completed / failed).
- `CognitiveWorker` (polls every 10s via BackgroundTaskScheduler).
- `unified_tasks` + links for kanban board state.
- Direct function calls + ToolGateway for side effects.

### Rationale
- Single cognition worker process today → table polling is trivial, exactly-once easy to reason about with DB transactions.
- Keeps deployment to one SQLite file + Docker Compose (no redis container, no persistence story, no networking).
- All observability (tokens, cost, duration) already written to same DB via public helpers.
- Eventual consistency and fan-out needs are met by `task_events`, `activity_log`, `family_insights`.

### Trigger Condition for Revisiting
Introduce Redis Streams (or equivalent) when:
- Multiple concurrent cognition workers / replicas are required (horizontal scale or HA).
- We need at-least-once delivery with consumer groups + dead-letter + offset tracking beyond what a simple claimed_at column provides.
- Cross-host or multi-machine deployment (currently all roles share LAN volumes).
- Activity/event feed requires pub/sub fan-out to many websocket clients at high frequency.
- External services must react to Bernie events without polling the DB.

At that point the `cognitive_tasks` table becomes a compatibility layer or is migrated into streams.

---

## ADR: Phase 4.4 thin facade (`claude_service.py`)

**Status:** Accepted (shipped `feat/perf-optimization`, 2026-06-24)

### Context
`claude_service.py` historically owned chat loop, context build, model state, shadow hooks, and test-compat shims (~300+ lines). Phase 4.4 carved logic into `bot/llm/` but left circular imports (`llm/chat.py` → facade) and blocked perf work on `build_context`.

### Decision
- **Production code** imports from `llm/*`, `constants`, `model_registry`, `tool_gateway` directly.
- **`claude_service.py`** is a **~60-line re-export facade** for transitional test patches and `main.py` `_init` shim only. No new business logic.
- **`llm/runtime.py`** owns `_container`, `init()`, `get_db()`, `get_container()`.
- **`llm/context_builder.py`** owns `build_context()` + per-leg timing.
- **`llm/compat.py`** owns `execute_tool` / `find_task` legacy signatures.
- **`constants.py`** owns `ROLE_*`; **`model_registry.py`** owns `DEFAULT_MODEL`.

### Rationale
Breaks `llm/*` → facade import cycles; keeps one place for test monkeypatches during migration; aligns with ServiceContainer routing (`container.llm_for()` only).

### Rules still in force
- All tools through `ToolGateway.execute()`.
- Every LLM turn → `log_llm_turn` + token DB row.
- Chat failure fallback stays **Ollama only**.
- Do not add logic to the facade — extend `llm/*` instead.

---

## ADR: Calendar and weather TTL cache (1800s)

**Status:** Accepted (shipped `feat/perf-optimization`, 2026-06-24)

### Context
`build_context()` called Google Calendar on every turn (~1.3s). Weather fetches added up to ~800ms on cold miss. Family schedule questions are bursty; manual calendar edits are infrequent relative to read volume.

### Decision
- **`context.calendar_cache_ttl_s`** and **`context.weather_cache_ttl_s`**: default **1800** (30 min).
- Calendar cache in `calendar_service._fetch_events`; shared by tools and `build_context`.
- **`invalidate_calendar_cache()`** on Bernie `create_event` (and service API if exposed).
- **Intent-gated prefetch**: DMs and non-schedule turns skip calendar in system prompt unless `llm/context_legs.should_prefetch_calendar()` matches (schedule/school/sleepover patterns). Weather prefetch similarly gated.

### Staleness budget
30 min lag acceptable for manual Google edits; live schedule questions still use calendar **tools** (cache may hit same TTL). Bernie event writes invalidate immediately.

### Rollback
Set TTL keys to `0` or remove block + `/reload` — no schema migration.

---

## ADR: Turn perf instrumentation (`activity_log`)

**Status:** Accepted (shipped `feat/perf-optimization`, 2026-06-24)

### Context
Primary native LLM path was invisible in logs (shadow Smol `[Step N]` misled diagnosis). No decomposed E2E timing for family burst tuning.

### Decision
Log structured perf events to **`activity_log`** (public `database.py` helpers only):

| `event_type` | Source | Payload |
|--------------|--------|---------|
| `turn_timing` | `llm/turn_timer.py` | `setup_ms`, `context_ms`, `llm_ms`, `tools_ms`, `send_ms`, `total_ms`, `turn_id`, channel/person |
| `context_build` | `llm/context_builder.py` | per-leg `presence_ms`, `ha_ms`, `calendar_ms`, `weather_ms`, `calendar_cache_hit` |
| `llm_iteration` | `executors/native.py` | `step`, `prompt_hash`, `tokens_in`, `delta_tokens` |
| `llm_queue` | `llm/queue.py` | queue depth, shed-shadow events |

**`token_usage.surface`**: `discord` | `shadow` | `shadow_harness` for cost split.

Instrumentation modules use **`db_binding.get_database()`**, not raw `import database`, so AST bypass scans stay clean.

### Rationale
Prod family traffic is the benchmark; no synthetic soak required. Admins query SQLite / Langfuse ad hoc; optional `#anvil` alerts not required.

---

## ADR: Discord reply chunking limits

**Status:** Accepted (shipped `feat/perf-optimization`, 2026-06-24)

### Decision
- **DMs:** chunk at **1900** chars (under Discord 2000 limit with embed/footer headroom).
- **Channels:** chunk at **3800** chars (under 4000 limit).
- Implemented in `bot.py` `_send_chunked` and long-reply paths.

### Rationale
Long LLM replies after 50s+ waits were failing silently (HTTP 400 / error 50035) — worse UX than slow replies.

---

## ADR: Shadow harness off during family peak

**Status:** Accepted (shipped `feat/perf-optimization`, 2026-06-24)

### Context
Smol shadow harness ran parallel tool loops with code-parse retries (observed 138k-token storms), competing with primary LiteLLM/OpenRouter during Child1 DM + Dad `#smithy` overlap.

### Decision
- **`eval.harness.enabled: false`** (preferred; legacy fallback `executor.shadow_harness_enabled`).
- **`eval.capture.enabled`** gates live shadow recording; **`eval.nightly.enabled`** gates overnight scoring — independent toggles (`/eval_mode`, `/nightly_eval`).
- **`eval.shadow_daily_cap: 10`** (down from 30).
- Keep **text-only model shadow** (single completion, no tool loop) when harness off.
- **Defer shadow** until post-reply + **`eval.capture.defer_s`** (legacy fallback `executor.shadow_defer_s`, default 2s).
- **LiteLLM queue** (`llm_queue_max_depth: 4`, shed shadow first via `eval.capture.shed_on_backpressure`).

### Rationale
Eval signal preserved at lower cost; family chat gets first shot at LiteLLM. Re-enable harness only for explicit eval experiments outside 15:00–21:00 ADT peak if needed.

**Superseded in part by** Shadow eval policy decoupling (2026-06-26) — prefer `eval.harness.*` and `eval.capture.*` keys; `harness_active()` enforces peak block at runtime.

---

## ADR: Shadow eval policy decoupling

**Status:** Accepted (shipped `feat/shadow-eval-decoupling`, 2026-06-26)

### Context
Shadow eval mixed concerns in one `eval.enabled` flag and `executor.shadow_*` keys: live capture, Smol harness triplets, nightly judges, HITL DMs, and ungrounded audit could not be tuned independently. `shadow_graduation.py` auto-promoted shadow models to primary based on score thresholds — routing changes are higher stakes than eval signal and bypassed human soak.

### Decision
1. **`bot/eval/policy.py`** — `resolve_eval_policy(config)` returns a frozen `EvalPolicy` dataclass; all capture, nightly, and hook paths read policy from here.
2. **Nested `eval` config** — `capture`, `harness`, `nightly` blocks with independent booleans (see CLAUDE.md eval table). Legacy `eval.enabled` and `executor.shadow_harness_enabled` / `shadow_defer_s` / `llm_queue_shed_shadow_first` remain **read fallbacks** only; admin tools and slashes write nested keys.
3. **`harness_active(policy)`** — harness runs only when `harness.enabled` **and** (if `block_peak_hours`) outside local peak window (`config.timezone`, default 15:00–21:00 ADT).
4. **Capture path** — `maybe_fire_shadow` → pair (`fire_shadow_call`) when harness inactive, triplet (`fire_shadow_triplet`) when active; both respect `shadow_daily_cap`, defer, and queue shed via policy.
5. **Nightly path** — `nightly_eval_worker` gates pair scoring, triplet scoring, HITL DMs, and ungrounded audit separately from `policy.nightly_*`.
6. **Remove shadow graduation** — delete `shadow_graduation.py`, daily graduation task, and APPROVE/REJECT/SNOOZE auto-promote flow. Model switches stay manual (`/model`, `litellm_switch_model`) informed by eval/HITL.

### Rationale
- Family peak: capture cheap text-only shadow; harness off by default.
- Ops: turn off HITL DMs without stopping capture; run ungrounded audit without triplet scoring.
- Safety: no automatic primary-model flip from shadow win rates.

### Admin surface
`/eval_mode` · `/nightly_eval` · `/harness_mode` · `/eval_scoring` · `/hitl_mode` · `/eval_status` — mirrored as `@tool` handlers in `tools/admin.py`. `/shadow_mode` remains exempt (Bernie must not change his own comparison model).

---

## ADR: Tool surface pipeline (Phase 39)

**Status:** Accepted (shipped `feat/phase-39-tool-surfaces`, 2026-06-27)

### Context
Every chat turn sends tool JSON schemas to the model. Modes filter by `domains.allow`, but:
- Concierge could advertise ~61 tools per turn (prompt cost + selection noise).
- `domains.deny` was not applied in `BernieContext.allowed_domains`.
- Mode/channel domain typos failed silently (empty surface).
- `chat_meal_planning` bypassed domain filter (~61 tools on `#furnace`).
- Narrow surfaces could hide `list_slash_commands` and block self-serve discovery.

### Decision
1. **`bot/llm/tool_surface.py`** owns Layer 2 surface math: `mode_ceiling` → `apply_channel_map` → `resolve_tool_domains` (delegates narrow to `intent_router.narrow_tool_domains`).
2. **Discovery union** — `get_tool_schemas_for_turn` always adds tools listed in `tool_surface.discovery_tools_always_on` by **name** (`search_tools`, `list_slash_commands`, `describe_modes`), then re-sorts alphabetically.
3. **Boot validation** — `validate_tool_surface_at_startup` fails on unknown domains in mode files / `channel_tool_domains` / broken mode YAML / missing discovery tool names.
4. **UX injection** — `append_tool_surface_ux` adds `active_surface_summary` + `deferral_system_block` when intent **or** channel map shrinks vs mode ceiling (dynamic system blocks, not edits to mode markdown files).
5. **Optional `channel_tool_domains`** — intersect with mode ceiling; hard bypass for `#anvil`; DMs never mapped. Pilot: conservative `#slag` map (no tasks/kanban in v1).
6. **Observability** — Langfuse `tools_advertised`, `tool_domain_count`, `mode`; `activity_log` `tool_surface` with `narrowed` flag.

### Rationale
- RBAC unchanged — narrowing is prompt-only; ToolGateway still enforces role on execute.
- Discovery tools prevent “impossible” replies when capability exists elsewhere.
- Channel map + mode ceiling compose without new mode files per channel.
- Sorted union preserves KV-cache stability contract from `ToolGateway`.

### Out of scope (39.0)
Vector tool retrieval, FTS5 for `search_tools`, RBAC changes, prompt layer budgets (39.1).

---

See also:
- docs/adr/0003-single-writer-sqlite.md (earlier snapshot of writer ownership)
- docker-compose.yml (bind mount comments)
- bot/worker.py, bot/database.py (claim/complete implementation)
- EVENT_BUS_FLOW.md (documents the *actual* implemented topology vs. planning fiction)
