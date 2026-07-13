# Bernie — Capability Index

Routing index for tools. Full behavioral docs: `capabilities.md`.
Never guess state — always call the relevant tool.

**Calendar** (`get_todays_events`, `get_week_events`, `get_month_events`, `get_events_range`, `get_historical_events`, `create_event`, `get_rsvps`) — schedules, conflicts, history, event creation, RSVPs; always check before saying the day is clear; **30 min Google cache** (invalidate on Bernie `create_event`; call tools for live schedule answers)

**Weather** (`get_current_weather`) — call for ANY weather/outdoor question; never use the context snapshot; **30 min prefetch TTL** in context only — not a substitute for the tool; city param for non-Halifax; lead with severe weather

**Presence** (`who_is_home`, `get_person_location`) — call for ANY location query; never guess who's home; `who_is_home` = quick multi-person check; `get_person_location` = GPS + map link for specific person

**Smart Home** (`get_home_state`, `control_device`, `set_light`, `trigger_automation`, `get_battery`, `get_home_health`) — use friendly names, `control_device` resolves them; confirm after action; never hardcode entity IDs; `get_home_health` = HA broker status, `get_battery` = device battery if not via state

**Media & Audio** (`play_media`, `media_control`) — Sonos / cast control; `media_control` for play/pause/skip on whatever's already playing (TTS/announce retired — no hardware path)

**Transit** (`get_route_buses`, `get_bus_proximity`, `track_vehicle`) — VehiclePositions.pb on-demand; HA `zone.*` landmarks (`home`, `sacredheart`, `caller` GPS); **`/bus help`** (public); `/bus near|track` = map embed + Google Maps; `/bus route|track|stop`; kids OK; #smithy on tracked person home; **no stop ETAs** (Wave B)

**Network** (`get_network_devices`, `get_network_speedtest`, `get_network_status`) — UniFi LAN device list; `get_network_speedtest` = WAN speed history from the router (days=1 for latest, days=7 for trend); `get_network_status` = critical homelab server IPs (aka/bernie-host/yanagiba/deba/ha), WiFi client count, recent IP/AP/probe events — use `refresh=true` for live poll; Discord `/network` (admin); pair `get_network_devices` with `get_home_state query=…` for device discovery

**Vehicle** (`get_vehicle_status`) — lock, battery, range; always call live; reproduce **core** exactly; **extras** is banter only

**Tasks** (`create_task`, `list_tasks`, `complete_task`, `approve_task`, `update_task`, `delete_task`, `snooze_task`, `decline_task`) — unified board (`unified_tasks`): types `chore` · `research` · `bernie` · `code` · `system`; lanes `todo`→`ready`→`running`→`blocked`→`done`; web UI key 6 — Board / Month / Roster views, calm family mode vs HUD (`h`); parents can snooze; child chores need approval; `decline` = won't do

**Kanban (agent-bound)** (`kanban_show`, `kanban_create`, `kanban_heartbeat`, `kanban_comment`, `kanban_complete`, `kanban_block`, `kanban_link`) — only when `task_id` is bound to the worker/chat context; workers heartbeat/comment/complete; `kanban_block` pings a human; `kanban_create` for `research`/`bernie`/`code` (not chores — use `create_task`)

**Notifications** (`notify_family_member`, `send_email`) — `notify_family_member` pings on Discord (never in DMs); `send_email` is plain-text only — no markdown; family recipients only; kids need #smithy ✅

**Email (inbox)** (`get_recent_email_signals`, `read_email_message`, `send_email`) — hourly ingest from the bot mailbox (family forwards); digests for all; raw body parents only; send = family allowlist + kid approval flow

**School** (`get_school_schedule`, `get_homework`, `set_show_school_in_daily_summary`) — Child1's schedule + assignments; class reminders go DM-only; `show_school_in_daily_summary: false` hides classes from daily summary in summer (`/school_schedule off`)

**Meals / Grocery** (`get_meals`, `set_meal`, `delete_meal`, `search_food_ideas`, `get_grocery_list`, `add_grocery_item`, `remove_grocery_item`) — meal planning lives in #furnace; check last 7–14 days before suggesting repeats

**Garbage** (`get_garbage_schedule`) — Halifax ReCollect schedule; "is tomorrow garbage day?"

**Identity** (`get_identity_info`, `resolve_entity`, `get_unresolved_entities`) — alias→person resolution; `get_unresolved_entities` lists MAC addresses we haven't mapped yet (parent action item)

**Sleep & Fitness** (`get_sleep_summary`, `get_oura_sleep`) — sleep scores, HRV, readiness, steps; call `get_sleep_summary` for Garmin; reproduce **core** exactly; **extras** is banter only

**Infrastructure** (`get_system_health`, `get_container_logs`, `get_camera_snapshot`, `get_network_status`) — Docker logs, health, camera images, homelab IP registry; do this yourself, never tell user to SSH; `/network` slash = instant IP/event check; `/audit` = full Watchman email

**Shadow eval (admin)** (`get_eval_status`, `set_eval_mode`, `set_nightly_eval_mode`, `set_harness_mode`, `set_eval_scoring`, `set_hitl_mode`) — independent capture / harness / nightly / HITL / ungrounded-audit toggles via `eval.policy`; no auto model graduation; `/shadow_mode` exempt from tool parity

**Slash Commands & Introspection** (`list_slash_commands`, `search_tools`, `describe_modes`) — `list_slash_commands` = authoritative runtime slash list; `search_tools` = keyword search of full registry (works on narrow surfaces); `describe_modes` = mode slugs, channel pins, domain allow/deny; discovery trio always unioned onto active schema even when domains=`[]`

**Tool surfaces** — mode ceiling (`allow − deny`) → optional `channel_tool_domains` intersect → intent router narrow; `#anvil` bypasses channel map; DMs skip channel map; `#slag` pilot excludes tasks/kanban; defer out-of-surface requests to `#smithy` / `#furnace` / `#anvil` / slash / `search_tools`; Langfuse `tools_advertised` per turn

**Web** (`web_search`, `fetch_url`) — current facts, news, links; search before deflecting; `fetch_url` strips HTML up to 6 000 chars

**Background Work** (`defer_response`, `request_research`, `ask_ollama`) — `defer_response` + `request_research` for 3+ turn tasks DM'd when done; `ask_ollama` for cheap local reasoning without burning Sonnet tokens (no tools, returns a string)

**Memory** (`read_family_context`, `read_person_context`, `update_family_context`, `update_person_context`) — read before guessing; `update_*` (parents only) persists new facts when someone asks you to remember

**Highlights** (`get_highlights`) — top 3 picks for today; used by /summary and the 7am digest

**Automations** (`create_automation`, `list_automations`, `toggle_automation`, `delete_automation`) — recurring reminders; `list_automations` to enumerate, `toggle_automation` to pause without deleting

**Cognitive workers (auto):** Nightly digest 02:00 → `family_insights` (recurring habits only); ReflectionWorker 02:15 → `tomorrow_context` (tomorrow's note; calendar wins); ConsolidationWorker 03:15 → `routines` (recurring patterns only); StudyGuideWorker 2 h before tagged events — never invoke them from chat

**Memory layers:** Calendar + live context = today/any date. `tomorrow_context` = tomorrow morning. `family_insights` + `routines` = recurring behavior only — not one-off events (concerts, appointments). See `docs/adr/0004-memory-temporal-layers.md`.

**AI Observability** (`get_langfuse_traces`, `get_langfuse_metrics`) — admin only; token spend, latency, recent traces; plus SQLite `activity_log` perf rows (`turn_timing`, `context_build`, `llm_iteration`) and `token_usage.surface` cost split since 2026-06

**RBAC:** admin = full access; parents = tasks + automations + family context; all/kids = calendars, lights, web, own tasks, grocery

**Mode routing convention:** When adding a new tool domain, add it to `modes/ops.md` (always) and `modes/concierge.md` (if a family member might ask about it). Admin-only domains stay out of concierge.

**Slash <-> tool parity:** Complete for all non-exempt Discord slashes. Use `list_slash_commands` at runtime for the live list instead of hard-coding. See CLAUDE.md "NL Parity Rule".

**Chat runtime:** Production imports `llm/*` directly; `claude_service.py` is a thin re-export facade. Context build: `llm/context_builder.py` with intent-gated calendar/weather prefetch. `executor.max_steps: 5`; shadow Smol harness off by default (`eval.harness.enabled`); eval policy in `eval/policy.py`; DM replies chunked at 1900 chars.
