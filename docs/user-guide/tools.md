# Tools reference

Human-readable catalog of Bernie's tools. Every tool passes through **ToolGateway** (RBAC, schema validation, audit logging).

**Role key:** `all` = everyone · `parents` = parents + admin · `admin` = admin only · `system` = internal workers

For slash command shortcuts, see [Slash commands](slash-commands.md).

---

## Calendar

| Tool | Role | Description |
|------|------|-------------|
| `get_todays_events` | all | Today's schedule |
| `get_week_events` | all | This week's events |
| `get_month_events` | all | Month view |
| `get_events_range` | all | Arbitrary date window |
| `get_historical_events` | all | Past events (up to ~1 year) |
| `create_event` | parents | Add Google Calendar event |
| `get_rsvps` | all | Attendee responses |
| `get_school_schedule` | all | Student class periods |
| `get_homework` | all | Assignments/tests from school calendar |
| `get_highlights` | all | Top picks for daily summary |
| `set_show_school_in_daily_summary` | parents | Summer break toggle |

**Needs:** Google Calendar OAuth (`token.json`).

---

## Weather

| Tool | Role | Description |
|------|------|-------------|
| `get_current_weather` | all | Live forecast (EC primary; Tomorrow.io optional cross-check) |

**Needs:** `lat`/`lon` in config; optional `TOMORROW_WEATHER_API`.

---

## Presence & location

| Tool | Role | Description |
|------|------|-------------|
| `who_is_home` | all | Quick home/away for household |
| `get_person_location` | all | GPS detail + map link |
| `get_battery` | all | Phone battery levels (HA sensors) |

**Needs:** Home Assistant person/device trackers.

---

## Smart home

| Tool | Role | Description |
|------|------|-------------|
| `get_home_state` | all | Query HA entities (discovery) |
| `control_device` | all* | Turn on/off, set brightness |
| `set_light` | all* | Light-specific control |
| `trigger_automation` | parents | Fire HA automation |
| `get_home_health` | admin | HA broker / integration status |
| `get_temperatures` | all | Temperature sensors |
| `list_ha_entities` | all | Search entities by domain/name |
| `ha_assist` | all | HA Assist pipeline (if configured) |
| `inspect_device` | admin | Deep device diagnostic |

\*Kids may be restricted by mode/RBAC for write actions.

**Needs:** `home_assistant` in config.

---

## Snapshots (exact numbers matter)

| Tool | Role | Description |
|------|------|-------------|
| `get_vehicle_status` | all | Lock, battery, range (curated entity map) |
| `get_sleep_summary` | all | Garmin sleep/HRV (curated map) |
| `get_oura_sleep` | all | Oura Ring data |

**Needs:** `snapshot_profiles` entity maps + HA or Oura token.

---

## Meals & grocery

| Tool | Role | Description |
|------|------|-------------|
| `get_meals` | all | Meal plan for date range |
| `set_meal` | parents | Set dish + notes |
| `delete_meal` | parents | Remove from plan |
| `search_food_ideas` | all | Spoonacular recipes |
| `get_grocery_list` | all | View categorized list |
| `add_grocery_item` | all | Add item |
| `remove_grocery_item` | all | Remove item |

**Needs:** `#furnace` channel for chef mode; optional `SPOON_API_KEY`.

---

## Tasks & automations

| Tool | Role | Description |
|------|------|-------------|
| `create_task` | parents | Chore or job on Kanban |
| `list_tasks` | all | Filter by person/status |
| `complete_task` | all | Mark done |
| `approve_task` | parents | Approve kid chore completion |
| `update_task` | parents | Priority, title, assignee |
| `delete_task` | parents | Remove task |
| `snooze_task` | all | Snooze reminder |
| `decline_task` | all | Won't do — notifies assigner |
| `create_automation` | parents | Recurring reminder |
| `list_automations` | all | Your automations |
| `toggle_automation` | all | Pause/resume |
| `delete_automation` | parents | Remove automation |

**Kanban (agent-bound):** `kanban_show`, `kanban_create`, `kanban_heartbeat`, `kanban_comment`, `kanban_complete`, `kanban_block`, `kanban_link`, `reassign_task` — used when a worker/chat session is bound to a task.

---

## Notifications & preferences

| Tool | Role | Description |
|------|------|-------------|
| `notify_family_member` | parents | Discord ping (not in DMs) |
| `send_email` | parents | Gmail send (plain text; family addresses) |
| `set_reminders` | all | `/reminders` parity |
| `set_dm_mode` | all | `/dm` parity |
| `get_settings` | all | `/settings` parity |

---

## Email (inbox)

| Tool | Role | Description |
|------|------|-------------|
| `get_recent_email_signals` | all | Summarized inbox digests |
| `read_email_message` | parents | Full message body by ID |

**Needs:** `gmail_token.json` + Bernie's mailbox configured.

---

## Transit (Halifax GTFS-RT)

| Tool | Role | Description |
|------|------|-------------|
| `get_route_buses` | all | Active buses on a route |
| `get_bus_proximity` | all | Nearest bus to HA zone landmark |
| `track_vehicle` | all | Track-until-home session |
| `stop_bus_tracking` | all | End tracking |

**Halifax default feed:** `gtfs.halifax.ca` VehiclePositions protobuf.

**Other cities:** integrate your agency's GTFS-RT feed — see [Optional integrations](../integrations/optional-services.md).

---

## Garbage (Halifax ReCollect)

| Tool | Role | Description |
|------|------|-------------|
| `get_garbage_schedule` | all | Upcoming curbside collection |

**Halifax:** parses municipal ReCollect ICS.

**Other cities:** replace ICS URL or parser for your waste provider.

---

## Flights

| Tool | Role | Description |
|------|------|-------------|
| `get_flight_status` | all | FlightAware live status |

**Needs:** `FLIGHT_AERO_KEY` in `.env`.

---

## Network & infrastructure

| Tool | Role | Description |
|------|------|-------------|
| `get_network_devices` | admin | UniFi client list |
| `get_network_speedtest` | all | WAN speed history |
| `get_network_status` | admin | Critical hosts + WiFi counts |
| `get_system_health` | admin | Host/container health |
| `get_container_logs` | admin | Docker log tail |
| `get_camera_snapshot` | all | Frigate still image |

---

## Media

| Tool | Role | Description |
|------|------|-------------|
| `play_media` | all | Sonos / cast |
| `media_control` | all | Play/pause/skip |

---

## Web & search

| Tool | Role | Description |
|------|------|-------------|
| `web_search` | all | SearxNG or configured search |
| `fetch_url` | all | Read a URL (stripped text) |

**Needs:** SearxNG or search endpoint in config for web search.

---

## Memory & introspection

| Tool | Role | Description |
|------|------|-------------|
| `read_family_context` | all | Household facts file |
| `update_family_context` | parents | Append family fact |
| `read_person_context` | all | Person-specific file |
| `update_person_context` | parents | Append person fact |
| `read_user_override` | all | Immutable human facts |
| `search_activity_log` | parents | FTS search of Bernie's event log |
| `session_search` | parents | FTS search of chat history |

---

## Identity

| Tool | Role | Description |
|------|------|-------------|
| `get_identity_info` | admin | Identity graph lookup |
| `resolve_entity` | admin | Alias → person/device |
| `get_unresolved_entities` | admin | Unknown MACs to map |

---

## Cognitive & background

| Tool | Role | Description |
|------|------|-------------|
| `defer_response` | all | "I'll DM you when done" |
| `request_research` | parents | Queue deep research worker |
| `ask_ollama` | admin | Cheap local reasoning (no tools) |
| `get_research_thread` | parents | Read research notes |
| `append_research_thread_note` | parents | Add research note |

**Automatic (not invoked from chat):** nightly digest, reflection, consolidation, study guide workers — see [FAQ](../help/faq.md#what-runs-at-night).

---

## Discovery & modes

| Tool | Role | Description |
|------|------|-------------|
| `list_slash_commands` | all | Runtime slash list |
| `search_tools` | all | Keyword search tool registry |
| `describe_modes` | all | Mode slugs and tool domains |
| `switch_mode` | admin | Override active mode |

---

## Admin & eval (`#anvil`)

| Tool | Role | Description |
|------|------|-------------|
| `reload_config` | admin | `/reload` |
| `litellm_switch_model` | admin | `/model` |
| `litellm_list_models` | admin | Model inventory |
| `litellm_add_model` | admin | `/model-add` |
| `litellm_remove_model` | admin | `/model-remove` |
| `set_config_summary` | admin | Summary schedule time |
| `set_config_reminders` | admin | Default reminder minutes |
| `frigate_set_mode` | admin | Alert mode |
| `frigate_set_camera` | admin | Per-camera enable |
| `frigate_set_hours` | admin | Night alert window |
| `trigger_system_audit` | admin | `/audit` |
| `get_eval_status` | admin | Eval pipeline status |
| `set_eval_mode` | admin | Shadow capture toggle |
| `set_nightly_eval_mode` | admin | Overnight judges |
| `set_harness_mode` | admin | Triplet harness |
| `set_eval_scoring` | admin | Scoring mode |
| `set_hitl_mode` | admin | Eval HITL DMs |
| `set_worker_model` | admin | Worker model |
| `get_langfuse_traces` | admin | Recent LLM traces |
| `get_langfuse_metrics` | admin | Token/cost metrics |
| `get_usage_costs` | admin | Usage summary |
| `config_doctor` | admin | Config hygiene report |
| `reset_web_pin` | admin | Reset dashboard PIN |

---

## Tool access by mode

Modes filter which **domains** Bernie advertises:

| Mode | Typical channel | Focus |
|------|-----------------|-------|
| **concierge** | `#smithy`, DMs | Full family surface (no admin) |
| **chef** | `#furnace` | Meals + grocery |
| **ops** | `#anvil` | Full admin surface |
| **security** | `#security`, alerts | Cameras + presence |
| **wind-down** | Quiet hours | Brief, calm replies |
| **tutor** | DMs (keyword) | Homework help |
| **chat-openwebui** | Web UI | OpenWebUI path |

Details: [Channels & modes](channels-and-modes.md).

---

## HITL tiers (writes)

| Tier | Behavior |
|------|----------|
| **1** | Silent proceed (reads) |
| **2** | Proceed + `#anvil` audit note |
| **3** | Hold → admin DM Approve/Deny |

Tier catalogue covers ~90 tools — destructive writes and outbound comms are gated.
