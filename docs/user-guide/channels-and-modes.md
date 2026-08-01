# Channels & modes

Bernie routes each message through a **channel** (Discord) and a **mode** (personality + tool surface). Both are config-driven.

---

## Discord channels

Create these (names are suggestions) and paste **channel IDs** into `config.json`:

| Config key | Forge name | Neutral | Bernie behavior |
|------------|------------|---------|-----------------|
| `schedule_channel_id` | `#smithy` | `#main` | Home channel — summaries, presence, general chat |
| `anvil_channel_id` | `#anvil` | `#admin` | Admin only — `/model`, `/reload`, eval |
| `furnace_channel_id` | `#furnace` | `#meals` | Meal planning — chef mode |
| `slag_channel_id` | `#slag` | `#ai-chat` | Extended AI chat (broader tools pilot) |
| `security_channel_id` | `#security` | `#security` | Frigate / camera alerts |
| `bellows_channel_id` | `#bellows` | `#chatter` | Family chit-chat — **bot stays silent** |

Restrict `#anvil` in Discord permissions to parents/admins.

Setup: [Discord onboarding](../discord-onboarding.md)

---

## Direct messages

Bernies responds in DMs when `dm_mode` is on (default). DMs:

- Skip some channel tool maps
- Never use `notify_family_member` (you're already talking 1:1)
- May skip calendar prefetch unless schedule keywords appear

Personal prefs: `/dm`, `/reminders`, `/settings`

---

## Modes

Modes live in `bot/modes/*.md` (YAML frontmatter + prompt). Bernie picks one per turn.

### Available modes

| Slug | When active | Tool focus |
|------|-------------|------------|
| **concierge** | Default — main channel, DMs | Full family surface (calendar, home, tasks, transit, …) |
| **chef** | `#furnace` pinned | Meals, grocery, recipes |
| **ops** | `#anvil` default | Everything including admin |
| **debug** | `#anvil` + "debug" keyword | Verbose diagnostics |
| **security** | Frigate alerts, security channel | Cameras, presence |
| **wind-down** | Quiet hours (config) | Brief, calm tone |
| **tutor** | Actor + homework keywords | School tools, study help |
| **home_automation** | HA-heavy keywords | Device control focus |
| **chat-openwebui** | Web UI path | OpenWebUI chat surface |

### Resolution order (highest wins)

1. Explicit override (`/mode` or `switch_mode` tool)
2. OpenWebUI flag
3. Event-driven (security alerts)
4. Channel pin (`#furnace` → chef, `#anvil` → ops/debug)
5. Actor + keyword (tutor)
6. Quiet hours → wind-down
7. Default → concierge

Admin can force a mode in `#anvil`:

```
/mode chef
/mode clear    # back to auto
```

---

## Tool domains

Each mode declares allowed **domains** (calendar, home, admin, …). ToolGateway intersects:

```
mode ceiling (allow − deny)
  → optional channel_tool_domains map
  → intent router (optional narrow)
```

| Rule | Effect |
|------|--------|
| `#anvil` | Bypasses channel tool map |
| DMs | Never channel-mapped |
| `#slag` | Often narrower (e.g. no tasks) — check your config |
| Discovery tools | `search_tools`, `list_slash_commands`, `describe_modes` always available |

Ask Bernie: "describe modes" or use the `describe_modes` tool.

---

## Quiet hours

`quiet_hours` in config (default ~22:00–07:00 local):

- Normal notifications → queue until morning
- High priority → still deliver
- Chat tone → wind-down mode

`/reminders on` flushes queued notifications.

---

## Channel-specific tips

### `#smithy` (main)

- Morning summary posts at `summary_hour` / `summary_minute`
- Presence announcements when people arrive/leave (if configured)
- Best for "what's today look like?"

### `#furnace` (meals)

- Chef mode — Bernie expects food/grocery language
- Calendar prefetch often skipped here (perf)

### `#anvil` (admin)

- Model changes, config reload, eval toggles
- Keep family chatter out — reduces accidental admin tool calls

### `#security`

- Frigate person/object alerts
- Snapshots on alert

### `#bellows`

- Bernie does **not** reply — family banter only

---

## Web UI

OpenWebUI integration uses **chat-openwebui** mode when configured (`openwebui_url`, `openwebui_users`). Separate from Discord routing.

Dashboard at `:8000` uses the API role — see [Web dashboard](web-dashboard.md).

---

## Adding a new tool domain

When you fork Bernie and add tools:

1. Register domain in `tools/__init__.py`
2. Add domain to `modes/ops.md` (always)
3. Add to `modes/concierge.md` if family-facing
4. Update [Tools reference](tools.md)

Convention from project docs — keeps surfaces predictable.
