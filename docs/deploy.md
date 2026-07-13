# Deploy Guide

Step-by-step guide for a fresh Bernie deployment.

**New users:** start with [docs/README.md](README.md) → [Quickstart](getting-started/quickstart.md).

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| Docker + Docker Compose | v2.20+ recommended |
| Python 3.11+ | Host only — one-time `scripts/auth_*.py` (see [google-oauth.md](google-oauth.md)) |
| Discord bot token | [discord-onboarding.md](discord-onboarding.md) |
| Anthropic API key | `console.anthropic.com` |
| Google Cloud project | Calendar (+ optional Gmail) — [google-oauth.md](google-oauth.md) |
| Home Assistant | Optional but recommended |

---

## 2. Discord Setup

Full walkthrough with checklist: **[discord-onboarding.md](discord-onboarding.md)**.

Short version: Developer Portal → bot token → Message Content Intent → invite URL with `bot` + `applications.commands` → copy server/channel/user snowflakes into `config.json`.

---

## 3. Clone the Repo

```bash
git clone <your-repo-url> family-bot
cd family-bot
```

---

## 4. Environment File (`.env`)

Copy the example and fill in every value:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | **Yes** | Bot token from §2 |
| `ANTHROPIC_API_KEY` | **Yes** | Anthropic console key |
| `LTE_LLM_MASTER_KEY` | Optional | LiteLLM proxy admin key |
| `SPOON_API_KEY` | Optional | Spoonacular — meal search |
| `TOMORROW_WEATHER_API` | Optional | Tomorrow.io — weather cross-check |
| `GOOGLE_CREDENTIALS_FILE` | Calendar: **yes** | OAuth client JSON — see [google-oauth.md](google-oauth.md) |
| `GOOGLE_TOKEN_FILE` | Calendar: **yes** | From `scripts/auth_google.py` |
| `GMAIL_TOKEN_FILE` | Optional | From `scripts/auth_gmail.py` — email features |
| `LANGFUSE_PUBLIC_KEY` | Optional | AI observability |
| `LANGFUSE_SECRET_KEY` | Optional | AI observability |
| `LANGFUSE_HOST` | Optional | Self-hosted Langfuse URL |
| `OURA_TOKEN` | Optional | Oura Ring personal access token |

---

## 5. Google OAuth (Calendar + optional Gmail)

**Canonical guide:** [google-oauth.md](google-oauth.md) — GCP project, test users, two-token model, troubleshooting.

Each household creates **their own** Desktop OAuth client. You never use the maintainer's `credentials.json`.

```bash
mkdir -p credentials
pip install --user google-auth-oauthlib google-api-python-client
python scripts/auth_google.py          # → credentials/token.json
python scripts/auth_gmail.py           # optional → credentials/gmail_token.json
```

Stub reference: `credentials/credentials.json.example` and [credentials/README.md](../credentials/README.md).

---

## 6. `config.json`

Copy the example and fill in your values:

```bash
cp config.example.json config.json
```

### config.json is gitignored — backup before `git pull`

Commit `88636f6` (github-readiness, 2026-06-20) added `config.json` to `.gitignore`. On a machine that had been tracking the file, a subsequent `git pull` **removed** `config.json` from the working tree even though the bot still needs it at runtime.

**Recovery from git history** (last tracked version before the ignore):

```bash
git show '88636f6^:config.json' > config.json.recovered
# Review, merge secrets/IDs from your off-git backup, then:
mv config.json.recovered config.json
```

**Do not rely on `cp config.example.json config.json` alone** for an existing production deployment — the example is a sanitized template and omits family-specific blocks (HA entity maps, calendars, `network_watchman.critical_hosts`, Frigate camera labels, etc.). Always keep dated backups **outside git** (e.g. `config.json.bak-YYYYMMDD` on the host or in your secrets store) before pulling or editing.

After restoring or merging config, restart Bernie or run `/reload` in `#anvil` and confirm startup logs (tool surface validation, Frigate MQTT, eval policy).

### CORS (`cors_origins`)

- **Unset / `[]`**: same-origin only. Fine when the dashboard is served by `bernie-api` on `:8000`.
- **Reverse proxy / alternate hostname / LAN origin**: set an explicit list, e.g. `["https://bernie.lan", "http://192.168.1.X:8000"]`.
- **`"*"` is refused at runtime** (empty allowlist) and flagged by config doctor — never use open CORS in production.

### Context prefetch (`context.prefetch`)

| Key | Default | Notes |
|-----|---------|--------|
| `calendar` | `intent` in code; example uses `lazy` | Schedule keywords gate injection |
| `weather` | `intent` | Weather keywords |
| `ha` | `always` | Set `intent` only after reviewing `looks_home_intent` — bare “home”/“room” are intentionally **not** matched |

### SQLite nightly backup

`bernie-cognition` (RW `./data`) runs `sqlite_backup` at **03:45 local** (`VACUUM INTO` → `./data/backups/family_bot-YYYYMMDD.db`, default retain 14 days via `db_backup_keep_days`). Same-day re-run is a no-op. Directory is created automatically under the `./data` volume — no extra compose mount.

### Required fields

```jsonc
{
  "family_name": "The Smiths",
  "timezone": "America/Halifax",       // IANA tz name
  "guild_id": "YOUR_DISCORD_SERVER_ID",

  // Channel IDs — right-click channel → Copy ID (Developer Mode must be on)
  "schedule_channel_id": "YOUR_SMITHY_ID",
  "anvil_channel_id":    "YOUR_ANVIL_ID",
  "furnace_channel_id":  "YOUR_FURNACE_ID",
  "bellows_channel_id":  "YOUR_BELLOWS_ID",
  "slag_channel_id":     "YOUR_SLAG_ID",

  // Daily summary time (24-hour)
  "summary_hour": 7,
  "summary_minute": 0,

  // Home location (for weather, garbage day, etc.)
  "lat": 44.6488,
  "lon": -63.5752,
  "location": { "city": "Halifax", "province": "NS", "country": "CA" }
}
```

### Home Assistant

```jsonc
"home_assistant": {
  "url": "http://homeassistant.local:8123",
  "token": "YOUR_HA_LONG_LIVED_TOKEN"
}
```

Generate a long-lived token: HA → Profile → Long-Lived Access Tokens.

### Calendars

```jsonc
"shared_calendars": [
  {
    "id": "primary",
    "name": "Family",
    "include_in_summary": true
  }
],
"school_calendars": [
  {
    "id": "YOUR_SCHOOL_CALENDAR_ID",
    "student": "YOUR_CHILD_NAME"
  }
]
```

Calendar IDs: Google Calendar → Settings → click a calendar → scroll to **Calendar ID**.

### Models

```jsonc
"active_model": "claude-sonnet-4-6",
"webui_model":  "claude-sonnet-4-6",
"digest_model": "claude-haiku-4-5-20251001"
```

Local model routing via LiteLLM uses `or-*` prefixed model names (e.g. `or-llama3`).

### Eval / Shadow

```jsonc
"eval": {
  "enabled": true,
  "shadow_model": "or-your-local-model",
  "shadow_daily_cap": 20,
  "eval_model": "claude-haiku-4-5-20251001",
  "worker_model": "claude-haiku-4-5-20251001",
  "capture": { "enabled": true, "defer_s": 2, "shed_on_backpressure": true },
  "harness": { "enabled": false, "block_peak_hours": true, "peak_start_hour": 15, "peak_end_hour": 21 },
  "nightly": { "enabled": true, "score_pairs": true, "score_triplets": true, "hitl": true, "ungrounded_audit": true }
}
```

Set `capture.enabled: true` (or legacy `eval.enabled: true`) to record shadow pairs/triplets. Toggle harness via `eval.harness.enabled` (admin: `/harness_mode`); legacy `executor.shadow_harness_enabled` is read-only fallback when nested key is absent. Nightly scoring, HITL DMs, and ungrounded audit are independent under `eval.nightly.*` (`/nightly_eval`, `/hitl_mode`, `/eval_scoring`).

### MQTT (Frigate cameras)

```jsonc
"mqtt": {
  "host": "192.168.1.X",
  "port": 1883,
  "username": "YOUR_MQTT_USER",
  "password": "YOUR_MQTT_PASS"
}
```

Leave the block empty (`{}`) to disable Frigate alerts.

---

## 6.5 Perf follow-ups (brownfield migration)

After merging `feat/perf-optimization`, add explicit `context.*` keys to **production** `config.json` — `/reload` alone does not opt in when keys are missing (code defaults preserve legacy behavior).

| Key | Code default if missing | Recommended prod rollout |
|-----|-------------------------|--------------------------|
| `context.prefetch.calendar` | `"intent"` | Keep `"intent"` until lazy soak on DMs; then try `"lazy"` for `#smithy` token savings |
| `context.intent_router.enabled` | `false` | `true` after review — narrows tool surface on clear intent |
| `context.snapshot_enabled` | `false` | `true` — BTS + hot-path presence/HA reads |
| `context.slag_funnel.enabled` | `false` | `true` — nudge open-ended planning to `#slag` |
| `context.history_verbatim_tail` | `4` | `4` (was 6) |

See `config.example.json` for the full perf-optimal template (`calendar: "lazy"`, all flags on). Rollback any item via `/reload` after editing config.

**Soak metrics (lazy calendar):** Compare `activity_log` tool calls for `get_todays_events` / `get_week_events` on schedule-intent DMs before/after switching `prefetch.calendar` to `"lazy"`.

**Tool surface observability:** Each narrowed/router turn logs `activity_log` event `tool_surface` with `{tool_count, domains, narrowed}` for before/after token analysis. Langfuse generations include tags `tools_advertised:N` and `tool_domains:N` on chat turns (Phase 39).

---

## 6.6 Tool surfaces (Phase 39 — brownfield rollout)

Phase 39 adds optional surface shaping **independent** of the intent router. Discovery union works even when `context.intent_router.enabled` is `false`.

Add to production `config.json` (copy from `config.example.json`):

```json
"tool_surface": {
  "inject_active_surface_summary": true,
  "inject_deferral_rule": true,
  "discovery_tools_always_on": ["search_tools", "list_slash_commands", "describe_modes"]
},
"channel_tool_domains": {
  "YOUR_SLAG_CHANNEL_ID": ["calendar", "memory", "weather", "notify", "search"]
}
```

| Key | Default if missing | Notes |
|-----|-------------------|-------|
| `tool_surface.inject_active_surface_summary` | `true` | Summary block when surface narrowed |
| `tool_surface.inject_deferral_rule` | `true` | Deferral copy (`#smithy`, `#furnace`, `#anvil`, `search_tools`) |
| `tool_surface.discovery_tools_always_on` | built-in default in code | Tool **names** unioned onto every turn |
| `channel_tool_domains` | absent = no channel map | Intersect with mode ceiling; `#anvil` hard bypass; DMs skip |

**Startup validation:** Boot fails loudly on unknown domain in mode files, `channel_tool_domains`, or missing discovery tool name. Fix typos before `/reload`.

**Measure before tuning:**

```bash
docker compose -f docker-compose.monolith.yml run --rm family-bot python /scripts/measure_tool_surface.py
```

**`#slag` pilot (bernie-host):** Conservative first map — **no** `tasks` / `kanban`. Soak 1–2 weeks via Langfuse `tools_advertised` + `activity_log tool_surface` before adding domains.

**Rollback:** Remove or empty `channel_tool_domains` entry; set `inject_deferral_rule: false` to disable deferral copy; `/reload`.

---

## 6.7 School calendar — summer break toggle

`show_school_in_daily_summary` controls whether events from `school_calendars` appear in **automatic** schedule surfaces (daily summary, `get_todays_events`, class reminder DMs). It does **not** block `/school`, `/homework`, or `get_school_schedule`.

| Key | Code default if missing | Typical use |
|-----|-------------------------|-------------|
| `show_school_in_daily_summary` | `true` | Set `false` June–August so summer class imports stay out of `/summary` |

**Toggle without editing JSON:** `/school_schedule off` or `/school_schedule on` (parents/admin). Bernie tool: `set_show_school_in_daily_summary`.

Brownfield installs without the key keep showing school (legacy behavior). `config.example.json` uses `true` so new installs match the code default.

---

## 7. Family Members

See the dedicated guide: [family.md](family.md)

---

## 8. Optional: LiteLLM Proxy

Bernie routes `or-*` models through a LiteLLM proxy for local model access (Ollama).

```jsonc
"litellm_base_url": "https://litellm.example.local",
"litellm_admin_url": "https://litellm.example.local:4000",
"ollama_base_url":   "http://192.168.1.X:11434",
"ollama_models": ["llama3.2", "mistral-nemo"]
```

If you're using a self-signed cert (e.g. Caddy on LAN), add it to the host trust store and mount it in `docker-compose.yml`. The compose file already has the hooks for this — update the volume paths.

---

## 9. First Run

```bash
docker compose up -d
docker compose logs -f family-bot
```

Watch for:
- `✓ Discord connected` — bot is online
- `✓ Commands synced` — slash commands registered
- `✓ Google Calendar OK` — calendar connected
- `✓ HA connected` — Home Assistant reachable (if configured)

Bernie will post a startup summary in `#smithy`.

### Verify slash commands

In `#anvil`:
```
/reload          — reload config without restarting
/eval_status     — check shadow eval state
/model           — show active model
```

---

## 9b. Host operations (tests + compose)

Production Bernie runs on **`bernie-host`**. SSH to the host first — compose and tests do not run from your laptop against the live stack unless you have an equivalent local clone.

```bash
ssh operator@bernie-host
cd /opt/family-bot
```

**Docker** — from that shell at the repo root, not from inside a container:

```bash
docker compose up -d
docker compose logs -f bernie-discord
docker compose up -d --build
```

**Tests** — on bernie-host, **not** `docker exec … python -m unittest` into production containers. Test failures write to `/data/bot.log` and show up as false positives in Watchman nightly audits.

```bash
ssh operator@bernie-host
cd /opt/family-bot
PYTHONPATH=bot python -m unittest discover -s bot/tests -p 'test_*.py' -v

# Or with project venv (common on bernie-host):
PYTHONPATH=bot my_venv/bin/python -m unittest discover -s bot/tests -p 'test_*.py' -v

# Targeted (eval / shadow)
PYTHONPATH=bot my_venv/bin/python -m unittest bot.tests.test_eval_policy bot.tests.test_shadow_hooks -v
```

`bot/tests/__init__.py` redirects test log output to a temp file when tests import through `bot.tests.*`. Linting (`ruff`) also runs on the host — it is not installed in the container image.

---

**Bot online but not responding**
- Check `DISCORD_TOKEN` is correct
- Confirm Message Content intent is enabled in developer portal

**Google Calendar auth loop**
- Delete `credentials/token.json` and re-run `scripts/auth_google.py`
- Confirm the OAuth client type is **Desktop**, not Web

**`litellm.example.local` not resolving inside container**
- Add `extra_hosts` to `docker-compose.yml` with the actual LAN IP:
  ```yaml
  extra_hosts:
    - "litellm.example.local:192.168.1.X"
  ```

**HA requests failing with SSL error**
- If HA uses a self-signed cert, either set `"ha_ssl_verify": false` in config.json or add the cert to the host trust store

**Database locked**
- Only one container should run at a time. Check `docker ps` for duplicate containers.
