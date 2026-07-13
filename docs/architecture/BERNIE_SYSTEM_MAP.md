# Bernie System Map

**Scope:** Fact inventory of every service, host, integration, data flow, and component present in the codebase as of 2026-06-20.  
**Rule:** Only what is implemented and visible in source, config, docs, and docker files. No inference. Stubs and partials are explicitly marked.

## 1. Runtime Hosts / Containers

| Component | Role | Implemented In | Connection / Protocol | Notes / Flags |
|-----------|------|----------------|-----------------------|---------------|
| bernie-discord (or monolith) | Discord client, slash commands, presence polling/adapters, HA WS listener, frigate MQTT listener, internal post server (port 9000) | bot/main.py (roles), bot/bot.py, presence_service.py, ha_service.py, frigate_listener.py, main.py:_run_internal_post_server | Discord gateway (discord.py); outbound to HA WS/REST, Unifi, Frigate MQTT/HTTP; inbound none except internal net | Binds 9000 on bernie-net only for cross-container POSTs from cognition. Healthcheck on /tmp/bernie_discord_ready. |
| bernie-api (or monolith) | FastAPI web dashboard + OpenWebUI shim + chat threads + task board + camera snapshots | bot/api.py, bot/main.py (run_api_role) | HTTP 8000 (published); internal WS for live updates via ConnectionManager | Serves web/, /api/*, /api/chat, OpenAI-compatible /v1. Health: /api/health. |
| bernie-cognition (or monolith) | CognitiveWorker (polls cognitive_tasks), WatchdogWorker, BTS scheduled jobs (nightly_digest, reflection, consolidation, research, etc.), watchman audits | bot/main.py (run_cognition_role), worker.py, background_scheduler.py, supervisor.py, cognitive_workers/*, cognitive_handlers/*, eval/* | Outbound: model calls (Ollama/LiteLLM/Anthropic), internal POST to bernie-discord:9000, DB writes | Depends on bernie-discord healthy. Heartbeat /tmp/bts_heartbeat. CPU/mem limited in compose. |
| Monolith fallback | Single container running all three roles | docker-compose.monolith.yml + main.py | Same as above | Preserves 100% legacy behavior. |

Networks: `bernie-net` (bridge) used only in split compose. Extra host `litellm.example.local:192.168.1.X` injected for LiteLLM resolution inside containers (example; customize).

## 2. External / LAN Integrations

| Integration | Host/Endpoint | Protocol/Auth | Direction | Code Owner | Notes / Flags |
|-------------|---------------|---------------|-----------|------------|---------------|
| Discord | Discord gateway (via token) | discord.py websocket + REST | Bidirectional | bot.py, main.py | Channels: #smithy (YOUR_SMITHY_CHANNEL_ID), #anvil, #furnace, #slag, #bellows, #security. |
| Anthropic (primary) | api.anthropic.com (implied) | AsyncAnthropic + ANTHROPIC_API_KEY | Outbound | llm/clients.py, service_container | claude-* models direct. Fallback path exists to Ollama. |
| LiteLLM proxy | https://litellm.example.local (base + admin) | AsyncAnthropic shim + LTE_LLM_MASTER_KEY; Caddy CA mounted | Outbound | service_container, litellm_service.py, llm/clients.py | or-* models. Admin calls for model mgmt. extra_hosts required. |
| Ollama (local workers/fallback) | http://192.168.1.X:11434 (config.ollama_base_url + llm_fallback) | HTTP /api/chat (non-OpenAI wire) | Outbound | worker_shared.py (via cognitive), llm/ollama.py, activity_aggregator | ollama_models list + fallback. (Real IPs scrubbed for public repo; use your config.) |
| Home Assistant | http://192.168.1.X:8123 (config.home_assistant.host) | REST /api/states + WS /api/websocket (Bearer token) | Bidirectional (poll + subscribe) | ha_service.py | Full entity refresh on start, WS state changes wired to presence. Many entity lists in config (lights, switches, media, climate, automations, sensors, person.*). network_scanner_entity. |
| Frigate | http://frigate.lan:5000 | HTTP snapshots (/api/{cam}/latest.jpg) + aiomqtt to MQTT broker | Outbound (snapshots) + subscribe (events) | frigate_service.py, frigate_listener.py | MQTT host/port from config.mqtt. Cameras: cam_8, cam_18. night_hours gating. |
| UniFi | https://unifi.local (or 192.168.1.X) | REST /proxy/network/api/... (x-api-key = UNIFI_KEY) | Outbound | presence/adapters.py (UniFiPresenceAdapter), network_watchman.py | Client list (sta), device stats. ssl_verify configurable (default false in presence cfg). |
| Google Calendar | googleapis.com (via oauth) | google-api-python-client | Outbound | calendar_service.py | Family + school + shared calendars. Token/cred files in /credentials. |
| Gmail | googleapis.com/auth/gmail.send | google auth + SMTP? (email_service) | Outbound | email_service.py | send_email tool + notifications. gmail_token.json. |
| Tomorrow.io | api.tomorrow.io | TOMORROW_WEATHER_API | Outbound | weather_service.py | Secondary / cross-check for current + daily. |
| Environment Canada | api.weather.gc.ca | Public | Outbound | weather_service.py | Primary forecast source (citypage + swob). |
| Open-Meteo | geocoding-api.open-meteo.com | Public | Outbound | weather_service.py | Reverse geocoding for location labels. |
| Oura Ring | https://api.ouraring.com/v2 | OURA_TOKEN | Outbound | oura_service.py | Sleep/readiness. Used via get_oura_sleep tool + health_sleep prefetch. |
| Spoonacular | (via SPOON_API_KEY, not shown in source paths) | HTTP | Outbound | food_service.py | search_food_ideas. |
| Halifax ReCollect | (hardcoded ICS URL in config) | ICS fetch | Outbound | garbage_service.py | get_tomorrow_collection / next. |
| Halifax Transit | https://gtfs.halifax.ca/realtime/Vehicle/VehiclePositions.pb | GTFS-RT protobuf | Outbound (15s cache) | transit_service.py | No TripUpdates/ETAs. Landmarks from HA zones + aliases. |
| SearxNG | http://searxng.lan:8081 (config.searxng_url) | /search?format=json | Outbound | tools/search.py (web_search) | fetch_url also present (direct). |
| OpenRouter | https://openrouter.ai/api/v1 | Bearer (credits only) | Outbound | activity_aggregator.py | Only for credit balance; not chat. |
| Langfuse | LANGFUSE_HOST (self-hosted LAN) | LANGFUSE_* keys | Outbound | langfuse_logger.py (partial), llm/observability.py | log_generation + planned tool spans. See stubs. |
| Web dashboard consumers | http://<host>:8000 | HTTP + WS | Inbound (published) | api.py + static web/ | Today, Tasks, Activity, Cognition, Cameras. Auth via web_pin or BERNIE_API_TOKEN for some paths. OpenWebUI users map. |

## 3. Internal / Cross-Component

| Component | Role | Protocol | Direction | Notes |
|-----------|------|----------|-----------|-------|
| Internal Post (discord role) | /internal/post, /internal/hitl/notify | HTTP POST (9000, bernie-net only) + X-Internal-Auth | cognition → discord | Used by research delivery, eval, dead letters, HITL notify. Fails closed if INTERNAL_POST_SECRET unset. |
| ServiceContainer | Process-wide registry | In-memory | N/A | Wires db, calendar, ha, frigate, presence, network, identity, notification_orchestrator, scheduler (BTS), cognitive_worker, anthropic/litellm/ollama clients, unified_tasks, stores, etc. |
| ConnectionManager (api) | Broadcast live updates to web WS clients | asyncio websockets | Internal | presence.update, chat.typing, etc. |
| Database (SQLite) | Single source of truth | aiosqlite (WAL, one conn + lock) | All roles share via bind-mount ./data | family_bot.db. Public API only (no raw _db_conn in tools). |
| ToolGateway | Single chokepoint | In-process call | Executors → handlers | RBAC + schema + tier/HITL + shadow block + lf + activity_log. |

## 4. Data Stores

- `data/family_bot.db` (SQLite, WAL): conversation_history, person_preferences, unified_tasks + links/executions/events, cognitive_tasks, family_insights, shadow_calls, pending_hitl, identity_nodes/aliases/edges, activity_log, notification_log, tomorrow_context, routines, task_outputs, presence_*, ha_devices, etc. Full schema in docs/db-schema.md.
- `data/memory.json`: acknowledged counts + missed-reminder patterns (injected to prompts).
- `data/network_devices.json`: MAC → name (checked before identity prompts).
- `data/research/`: (limited contents visible).
- Config: config.json (live-reloaded via mtime + /reload). Credentials separate.
- bot.log (file + stdout).

## 5. Background / Scheduling

- BackgroundTaskScheduler (BTS) + TaskSupervisor: wraps discord.ext.tasks.Loop. Registers with owner/tier. 3-restart then #anvil alert.
- CognitiveWorker: polls `claim_next_task()` every 10s (cognition role), dispatches via cognitive_handlers.registry (reflection, consolidation, research, study_guide, research_deliver, etc.).
- WatchdogWorker: 60s reclaim of stale + dead-letter.
- Scheduled via BTS: nightly_digest_loop (~02:00), reflection, consolidation, proactive_nudge, watchman audits, frigate dedup, presence, etc.
- HA WS + presence callbacks drive real-time arrive/depart (no heavy polling for main path).
- Frigate listener loop + MQTT for person/object alerts.

## 6. Tools Surface (via @tool decorator + registry)

All tools go exclusively through ToolGateway.execute(). Domains: admin, calendar, cognitive, home, identity, kanban, meals, media, memory, modes, network, notify, presence, search, snapshots, tasks, transit, weather.

Full list (extracted): get_todays_events, get_week_events, ..., create_event, control_device, get_home_state, get_oura_sleep, get_sleep_summary, get_vehicle_status, web_search, fetch_url, request_research, kanban_*, create_task/list_tasks/..., notify_*, get_person_location/who_is_home/..., get_current_weather, get_route_buses/get_bus_proximity/track_vehicle, get_network_status/get_network_speedtest, frigate_*/get_camera_snapshot, litellm_*, reload_config, get_system_health, get_container_logs, search_activity_log, update_*_context, switch_mode, send_email, play_media/media_control (TTS/announce retired), get_garbage_schedule, get_home_health/get_network_devices, get_identity_info/resolve_entity, get_rsvps/get_school_schedule/get_homework/get_highlights, ask_ollama/defer_response/get_research_thread/append_..., and ~20 more admin/task variants.

See bot/tools/*.py and docs/capabilities.md for descriptions + rules.

## 7. Stubs / Partial / Undocumented / Stale

- **TTS announce** (ha_service.py:418): "STUB: Hardware configuration pending". Announces logged only.
- **Langfuse tool spans** (langfuse_service.py): Explicit "no-op stub". log_generation path exists in llm/observability + executors; full spans "Plan B".
- **Redis Streams / NATS / ARQ event bus**: Referenced only in planning docs (claude_upgrade_plan.md, implementation-notes-28.html, activity_plan.md). "Redis migration deferred indefinitely." "cognitive_tasks table is sufficient". **No implementation**.
- **workers surface default**: `executor.workers` exists in config but "workers is dead" per CLAUDE.md + comments; cognitive path uses direct polling + PydanticAI, bypasses chat executors.
- **Child1 device_tracker**: Multiple entries in config (icloud, cloud); some may be stale or partial. person.child1 exists.
- **MQTT broker details**: host/port in config.mqtt; used by aiomqtt in frigate_listener only. No other producers/consumers visible.
- **OpenWebUI / webui_model / openwebui_url**: Configured and served via api shim; user mapping present. Full feature parity not enumerated in every path.
- **Pi-hole / network_watchman**: HA entity cross-check + Unifi probes present. Some HA sensors (octoprint etc.) appear in config but treated as generic.
- **Garmin**: Snapshot via HA sensors (get_sleep_summary / get_vehicle_status in snapshots.py). No direct Garmin API.
- **Email delivery**: Present (send_email + notify paths) but secondary; primary is Discord + DM.
- **Web pin hash**: Stored per family_member; reset tool exists.
- No AGENTS.md or similar at subdirs beyond root CLAUDE.md/CONTEXT.md.

Undocumented in running code but referenced in docs: exact Pi-hole entity names, all HA sensor lists (config is source of truth).

## 8. Data Flows Summary (High Level)

- Chat request (Discord/web): message → executor loop (native/smol) → ToolGateway → services/DB → model → reply.
- Background cognition: enqueue to cognitive_tasks → CognitiveWorker claim + handler (direct model + typed) → write task_outputs / post via internal / DM / email.
- Presence: UniFi poll/adapter + HA WS + GPS → fused state → callbacks + DB + broadcast.
- Alerts: Frigate MQTT listener → conditional notify (night/away gate) + snapshot to #security.
- Nightly: 02:00 digest (insights), 02:15 reflection (tomorrow_context), 03:15 consolidation (routines).
- Eval/audit: shadow calls stored, judges (PydanticAI), watchman log scan.

All side-effects (writes, notifies, control) go through ToolGateway (except pure worker output persistence and scheduled delivery).

## 9. Config-Driven Surfaces (key blocks)

- executor (chat/smol_models/native_intent_patterns/smol_intent_patterns/chat_routing)
- family_members + presence.device_trackers + discord_roles + permission_groups
- home_assistant (host + entities + automations + sensors)
- frigate, transit, presence, quiet_hours, cognitive_workers, tool_gateway (caps)
- active_model, ollama_*, litellm_*, digest_model, webui_model, vision_model, audit_model, etc.
- searxng_url, recollect_ics_url, shared_calendars, school_calendars

See config.json + config_validate.py for full structure. Reload mutates in place; some caches (model registry) refreshed async.

**End of map.** All entries labelled. Stubs disclosed. No Redis event bus present.
