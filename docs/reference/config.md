# config.json reference

Glossary of common `config.json` fields. Copy from `config.minimal.example.json` (hour-one) or `config.example.json` (full shape).

**`config.json` is gitignored** — never commit real snowflakes, tokens, or emails.

After edits: `/reload` in `#anvil` or restart containers.

---

## Core

| Key | Required | Description |
|-----|----------|-------------|
| `timezone` | Yes | IANA zone, e.g. `America/Halifax` |
| `family_name` | Yes | Display name in prompts |
| `guild_id` | Yes | Discord server ID |
| `admin_discord_id` | Yes | Primary admin user ID |

---

## Discord channels

| Key | Description |
|-----|-------------|
| `schedule_channel_id` | Main family channel (`#smithy`) |
| `anvil_channel_id` | Admin channel |
| `furnace_channel_id` | Meal planning |
| `slag_channel_id` | Extended AI chat |
| `security_channel_id` | Camera/security alerts |
| `bellows_channel_id` | Silent chit-chat channel |

See [Channels & modes](../user-guide/channels-and-modes.md).

---

## Family members

`family_members` — object keyed by display name:

| Field | Description |
|-------|-------------|
| `canonical_id` | Internal ID — matches `docs/{id}.md` |
| `first_name` | Display first name |
| `aliases` | Name variants for resolution |
| `discord_id` | 18-digit Discord user ID |
| `email` | For email send policy |
| `role` | `admin` · `parents` · `kids` · `friend` |
| `calendars` | Google Calendar IDs for this person |
| `device_macs` | Wi-Fi MACs for UniFi presence |
| `ha_entity` | HA `person.*` entity |
| `web_pin_hash` | bcrypt hash for dashboard PIN |

Guide: [Family setup](../family.md)

---

## Discord roles

```jsonc
"discord_roles": {
  "admin": "Admin",
  "parents": "Parents",
  "kids": "Kids"
}
```

Must match role **names** in your Discord server.

---

## Location & weather

| Key | Description |
|-----|-------------|
| `lat` / `lon` | Coordinates for weather |
| `location.city` | Display string in prompts |

Halifax deploys often use `America/Halifax` + local coordinates. Set for your city.

---

## Calendars

| Key | Description |
|-----|-------------|
| `shared_calendars` | Array of `{ id, name, alias?, include_in_summary? }` |
| `school_calendars` | Array of `{ id, student }` for student schedules |
| `show_school_in_daily_summary` | `false` hides classes in summer |

Requires Google OAuth — [Google OAuth](../google-oauth.md).

---

## Models & LLM

| Key | Description |
|-----|-------------|
| `active_model` | Default Discord chat model |
| `webui_model` | Web UI chat model |
| `digest_model` | Nightly digest model |
| `audit_model` | Watchman log audit |
| `worker_model` | Background cognitive workers |
| `anthropic_models` | Allowed Anthropic model list |
| `litellm_base_url` | LiteLLM proxy URL (optional) |
| `litellm_models` | Auto-discovered or listed proxy models |
| `ollama_base_url` | Local Ollama URL (optional) |
| `ollama_models` | Local model names |

---

## Home Assistant

```jsonc
"home_assistant": {
  "url": "http://homeassistant.local:8123",
  "token": "YOUR_LONG_LIVED_TOKEN"
}
```

Optional entity lists, automations, and `presence.device_trackers` in full example config.

---

## Summary schedule

| Key | Default | Description |
|-----|---------|-------------|
| `summary_hour` | 7 | Morning summary hour (local) |
| `summary_minute` | 0 | Morning summary minute |
| `weekly_summary_hour` | 8 | Weekly summary |
| `poll_interval_minutes` | 5 | Background poll interval |
| `default_reminder_minutes` | 30 | Event reminder lead time |

Admin can override summary time: `/config_summary`.

---

## Executor (chat behavior)

```jsonc
"executor": {
  "chat": "native",           // native | smol
  "max_steps": 5,             // max tool loop iterations
  "max_tokens": 4096,
  "chat_routing": "intent",   // multistep → smol when intent matches
  "llm_step_timeout_s": 45,
  "llm_queue_max_depth": 4
}
```

---

## Context & prefetch

```jsonc
"context": {
  "calendar_cache_ttl_s": 1800,
  "weather_cache_ttl_s": 1800,
  "prefetch": {
    "calendar": "intent",   // intent | lazy | always | never
    "weather": "intent",
    "ha": "always"
  },
  "intent_router": { "enabled": false }
}
```

---

## Tool gateway

```jsonc
"tool_gateway": {
  "max_result_chars": 6000,
  "per_tool_max_chars": { "web_search": 12000 },
  "calendar_summary_mode": true
}
```

---

## Tool surface (Phase 39)

```jsonc
"tool_surface": {
  "inject_active_surface_summary": true,
  "channel_tool_domains": {
    "YOUR_SLAG_CHANNEL_ID": ["calendar", "memory", "weather"]
  }
}
```

`#anvil` bypasses channel map. DMs never mapped.

---

## Email

```jsonc
"email": {
  "max_sends_per_hour": 10,
  "max_sends_per_domain_per_hour": 3,
  "approval_channel_id": "YOUR_MAIN_CHANNEL_ID"
},
"study_guide_cc_email": "parent@example.com",
"research_cc_email": "parent@example.com"
```

CC addresses must match a family member email.

---

## Frigate

```jsonc
"frigate": {
  "mode": "on",
  "cameras_enabled": { "front_door": true },
  "cameras": { "front_door": "Front Door" },
  "alert_labels": ["person"],
  "gate_entity": "person.parent_one",
  "night_hours": { "start": "22:00", "end": "07:00" }
}
```

---

## Presence & UniFi

```jsonc
"presence": {
  "unifi_host": "https://192.168.1.X",
  "unifi_ssl_verify": false,
  "polling_interval_seconds": 60,
  "device_trackers": {
    "parent_one": "device_tracker.phone"
  }
}
```

`UNIFI_KEY` in `.env` — not in config.json.

---

## Snapshot profiles

Curated HA entity maps for exact-number tools:

```jsonc
"snapshot_profiles": {
  "vehicles": { "family_car": { "entity_id": "..." } },
  "sleep": { "parent_one": { "entity_id": "..." } }
}
```

Empty `{}` in minimal config — fill for `get_vehicle_status`, `get_sleep_summary`.

---

## Eval (operators)

Nested under `eval` — shadow capture, nightly judges, harness, HITL. Defaults off or conservative in public example.

Toggle from `#anvil`: `/eval_mode`, `/nightly_eval`, etc.

---

## Quiet hours

```jsonc
"quiet_hours": {
  "start_hour": 22,
  "end_hour": 7
}
```

---

## CORS (dashboard)

```jsonc
"cors_origins": ["https://bernie.lan", "http://192.168.1.X:8000"]
```

Unset = same-origin only. `"*"` refused at runtime.

---

## Hot memory

```jsonc
"hot_memory": {
  "context_md_max_chars": 12000,
  "person_md_max_chars": 8000,
  "user_override_path": "USER_OVERRIDE.md"
}
```

---

## Internal (compose)

| Key | Description |
|-----|-------------|
| `internal_discord_url` | Cognition → discord internal post URL |

Usually set for three-container compose — see `config.example.json`.

---

## Full example

See `config.example.json` in repo root for Frigate, network watchman, eval, LiteLLM, and channel maps.

Validation runs at startup — bad tool domains or YAML in modes fail loud in logs.
