# Database Schema

SQLite at `/data/family_bot.db`.

**Nightly backup:** cognition BTS job `sqlite_backup` (03:45 local) writes `VACUUM INTO` copies under `/data/backups/family_bot-YYYYMMDD.db` (retain `db_backup_keep_days`, default 14). See `docs/deploy.md` § SQLite nightly backup.

**Brownfield migrations:** new tables/columns need an `ensure_*_schema()` on the `init_db()` early-return path, not only `CREATE TABLE` in the first-run script. See **Schema migrations (existing DBs)** in `CLAUDE.md`.

| Table | Purpose |
|-------|---------|
| `conversation_history` | Last 50 messages per channel |
| `conversation_history_fts` | FTS5 virtual table (migration **v8**, 5hy.11) — `session_search` |
| `sent_reminders` | Dedup for reminders, summaries, startup greeting |
| `rsvps` | Emoji reactions keyed by `(event_id, discord_id)` |
| `message_event_map` | Discord message ID → Google Calendar event ID |
| `person_preferences` | Canonical per-person prefs: `reminders_enabled`, `dm_mode`, `reminder_minutes`, quiet hours |
| `meals` | Meal plan: date, meal_type, dish, notes |
| `groceries` | Categorized grocery list |
| `memory_events` | Acknowledged/missed event log per person |
| `presence_current` | Latest home/away state per person |
| `presence_log` | Arrival/departure history |
| `ha_devices` | HA entities Bernie manages — last state snapshot |
| `notification_log` | All notifications sent — recipient, channel, success/failure |
| `activity_log` | Full Bernie event log — type, description, metadata |
| `token_usage` | Claude token spend per conversation |
| `weather_cache` | Reserved / unused |
| `weather_location_cache` | Geocoded city → lat/lon, display name, timezone; 30-day TTL |
| `family_insights` | Per-person nightly digest insights — recurring preferences/habits only; 14-day TTL or permanent; one-offs filtered in `insight_extraction.py` |
| `digest_log` | Dedup guard for nightly digest runs |
| `shadow_calls` | Eval pairs — primary + shadow response, prompt hash, judge scores |
| `pending_hitl` | Tier-3 tool approval holds — tool name, args, serialized ctx, status (`pending`/`approved`/`denied`/`expired`). Not `shadow_judgments` (eval HITL). |
| `identity_nodes` | Identity graph nodes — people, devices, canonical_id, metadata |
| `identity_aliases` | Alias → node_id with confidence and source |
| `identity_edges` | Relationships between nodes (e.g. owned_by) |
| `unresolved_entities` | Unknown MACs seen on network — logged for later resolution |
| `tasks` | **Removed** (40B-1f) — legacy chore table; rows live in `unified_tasks` after `migrate_tasks_v32` |
| `unified_tasks` | Canonical task board — type (`chore`/`research`/`bernie`/`code`/`system`), `kanban_status`, `horizon`, `acceptable_assignees`, heartbeat, run control |
| `task_links` | Parent→child DAG edges; children promote to `ready` when parents `done` |
| `task_executions` | Per-run history (`execution_id`, metrics, logs) for agent/research work |
| `task_events` | Append-only audit (`created`, `heartbeat`, `comment`, `reclaimed`, …) |
| `cognitive_tasks` | Worker queue; status: queued/active/done/dead_letter; cost cols: model_used, tokens_in/out, duration_ms, gpu_ms |
| `semantic_observations` | Distilled facts per person — confidence, optional expiry |
| `tomorrow_context` | ReflectionWorker output; per-date household + per-person notes for **tomorrow**; calendar is source of truth for dated events; feeds 7am summary |
| `routines` | MemoryConsolidationWorker output — **recurring** per-person habits only; one-off proposals rejected in code; confidence decay weekly |
| `task_outputs` | Worker results keyed by `(task_id, key)`; research threads use `thread:log` JSON on unified `type=research` task ids |
| `email_signals` | Phase 34 — ingested Gmail forwards: `gmail_id` (unique), `thread_id`, sender/forwarder emails + person ids, typed `summary`, `topics` JSON, debug headers |
| `email_pending` | Kid-initiated send drafts awaiting #smithy approval — `status` pending/sending/sent/denied/expired; optional `reply_to_gmail_id`, `thread_id` |
| `email_ingest_cursor` | Singleton Gmail `historyId` cursor for hourly inbox poll |
| `email_send_rate` | Sliding 1h send counters — one row per successful send (`requester_id`, `recipient`, `recipient_domain`, `sent_at`) |
| `schema_migrations` | Version bookkeeping — v1–7 (40B-2B), **v8** conversation_history_fts, **v9** activity_log event_type+logged_at index |

## Nightly memory pipeline

Local time (Halifax). Dated events live in Google Calendar — not in `routines`.

| Time | Component | Writes | Purpose |
|------|-----------|--------|---------|
| 02:00 | `nightly_digest.py` + `insight_extraction.py` | `family_insights` | Distill yesterday's chat into recurring behavioral notes |
| 02:15 | `ReflectionWorker` | `tomorrow_context` | Observational note for tomorrow; reads calendar + today's insights |
| 03:15 | `MemoryConsolidationWorker` | `routines`, `semantic_observations` | Promote reinforced patterns; reject one-off events as routines |

See [ADR 0004](adr/0004-memory-temporal-layers.md) for the temporal split rationale.
