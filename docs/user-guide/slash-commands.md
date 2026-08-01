# Slash commands

Every Discord slash command Bernie registers. Natural language works too — these are shortcuts and parity anchors.

**Discovery at runtime:** ask Bernie in `#anvil` to run `list_slash_commands`, or use `/settings` for personal prefs.

Channel names below use this repo's forge metaphors (`#smithy` = main). Only **numeric channel IDs** in `config.json` matter.

---

## Family commands (any allowed channel)

| Command | Options | What it does |
|---------|---------|--------------|
| `/summary` | — | Post today's schedule + highlights now |
| `/today` | — | Alias for `/summary` |
| `/week` | — | Post this week's schedule |
| `/weather` | `now` · `today` · `week` | Environment Canada forecast |
| `/school` | — | Today's full class schedule (student calendar) |
| `/homework` | `today` · `tomorrow` · `week` | Assignments and due dates |
| `/garbage` | — | Upcoming collection (Halifax ReCollect — [fork for your city](../integrations/optional-services.md)) |
| `/rsvps` | event name | Who confirmed for a calendar event |
| `/addevent` | — | Start event creation (Bernie asks for details) |
| `/setreminder` | — | Custom reminder lead time for an event |
| `/school_schedule` | `on` · `off` | Show/hide school classes in daily summary (summer break) |
| `/reminders` | `on` · `off` | Channel @mention reminders for you |
| `/dm` | `on` · `off` | Personal reminders via DM instead |
| `/settings` | — | View your Bernie preferences |
| `/task_add` | title, assignee, … | Create chore or personal task |
| `/task_list` | — | List your tasks |
| `/task_done` | task id | Mark complete |
| `/task_snooze` | task id | Snooze reminder |
| `/task_no` | task id | Decline / won't do |
| `/task_approve` | task id | Parent approves kid's completed chore |
| `/automation_add` | — | Recurring reminder automation |
| `/automation_list` | — | List your automations |
| `/automation_toggle` | id · on/off | Pause/resume automation |
| `/automation_delete` | id | Remove automation |
| `/temps` | — | Home temperature sensors (needs HA) |
| `/ha_entities` | domain filter | Search/list HA devices |
| `/speedtest` | count, live | UniFi WAN speed history |
| `/snap` | camera | Frigate camera snapshot |
| `/flight` | flight number | Live flight status (FlightAware) |
| `/bus` | subcommands | Transit — see below |

### `/bus` subcommands (Halifax Transit)

| Subcommand | What it does |
|------------|--------------|
| `/bus help` | Usage for route, near, track, stop |
| `/bus route` | All active buses on a route |
| `/bus near` | Nearest bus on a route to a landmark (HA zones) |
| `/bus track` | Track until home or destination |
| `/bus stop` | End your tracking session |

**Other cities:** requires a GTFS-RT feed and config — see [Optional integrations](../integrations/optional-services.md).

---

## Admin commands (`#anvil` only)

Restricted to admin/parent roles and the admin channel.

| Command | What it does |
|---------|--------------|
| `/model` | View or switch chat model |
| `/model-add` | Register OpenRouter model in LiteLLM |
| `/model-remove` | Remove model from LiteLLM |
| `/reload` | Reload `config.json` without restart |
| `/config_summary` | Change daily summary time (hour/minute) |
| `/config_reminders` | Default reminder lead (minutes) |
| `/mode` | View or switch operational mode |
| `/eval_mode` | Toggle shadow eval capture |
| `/nightly_eval` | Toggle overnight judge scoring |
| `/harness_mode` | Toggle expensive triplet harness |
| `/eval_scoring` | pair · triplet · both · none |
| `/hitl_mode` | Admin DMs on divergent eval |
| `/shadow_mode` | Set shadow comparison model (no tool parity — admin only) |
| `/worker_model` | Background worker model |
| `/eval_status` | Eval pipeline status + counts |
| `/audit` | Trigger system health report now |
| `/network` | Homelab IP registry + recent events |
| `/email` | Send email via Bernie's mailbox |
| `/frigate_mode` | `on` · `off` · `test` |
| `/frigate_camera` | Enable/disable per-camera alerts |

---

## NL parity

Every slash above (except `/shadow_mode`) has a matching `@tool` — Bernie can do the same from chat. Examples:

- `/weather today` ↔ "what's the weather this week?"
- `/task_add` ↔ "remind Jamie to take out the bins"
- `/reload` ↔ "reload config" (admin, `#anvil`)

Full tool list: [Tools reference](tools.md).

---

## Permissions recap

| Role | Typical access |
|------|----------------|
| **kids / all** | Calendar read, weather, tasks (own), grocery, lights (policy-dependent) |
| **parents** | Assign tasks, approve chores, automations, family context writes |
| **admin** | `#anvil` commands, model switch, logs, eval, network |

Details: [Security & roles](security-and-roles.md).
