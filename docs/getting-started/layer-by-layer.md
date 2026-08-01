# Layer by layer

Add Bernie capabilities **after** [Quickstart](quickstart.md) chat works. Each layer is optional except Discord + Anthropic (the base).

Skip anything you don't have hardware or accounts for — Bernie degrades gracefully.

---

## Layer 0 — Base (required)

| Piece | Config / secret | Unlocks |
|-------|-----------------|---------|
| Discord | `.env` `DISCORD_TOKEN` + `config.json` channel IDs | Chat, slash commands, DMs |
| Anthropic | `.env` `ANTHROPIC_API_KEY` | Replies, tool loops |
| Internal secret | `.env` `INTERNAL_POST_SECRET` | Cross-container writes |

**Verify:** message in main channel → reply.

---

## Layer 1 — Google Calendar

| Piece | Setup | Unlocks |
|-------|-------|---------|
| OAuth client | `credentials/credentials.json` | — |
| Calendar token | `python scripts/auth_google.py` → `token.json` | `/summary`, `/week`, event creation, school calendar |

Guide: [Google OAuth](../google-oauth.md)

**Verify:** `/summary` posts today's events.

**Config keys:** `shared_calendars`, `school_calendars`, `family_members.*.calendars`, `summary_hour` / `summary_minute`.

---

## Layer 2 — Family identity

| Piece | Setup | Unlocks |
|-------|-------|---------|
| `family_members` | Names, Discord IDs, roles, emails | RBAC, presence labels, email policy |
| Person context files | `docs/{canonical_id}.md` (optional) | `read_person_context` — preferences Bernie remembers |
| `USER_OVERRIDE.md` | Human-edited facts in `docs/` | Immutable household rules |

Guide: [Family setup](../family.md)

**Verify:** `/settings` shows your preferences; kid vs parent tool access differs.

**Tip:** `canonical_id` must match the filename — e.g. `dad` → `docs/dad.md`. Generic pack ships with `docs/family/dad.md` etc.

---

## Layer 3 — Home Assistant

| Piece | Setup | Unlocks |
|-------|-------|---------|
| HA long-lived token | `config.json` → `home_assistant` | Lights, switches, presence, zones, sensors |
| Entity maps | `home_assistant.entities`, `presence.device_trackers` | `control_device`, `who_is_home`, transit landmarks |

**Verify:** `/temps` or "turn off the kitchen light" (if entity exists).

**Config keys:** `home_assistant.url`, `home_assistant.token`, `presence.*`, `snapshot_profiles` (vehicle/sleep entity maps).

Without HA: presence, smart home, and HA-backed bus landmarks won't work — calendar and chat still do.

---

## Layer 4 — Weather location

| Piece | Setup | Unlocks |
|-------|-------|---------|
| Coordinates | `lat`, `lon`, `location` in config | Accurate local weather |
| Tomorrow.io (optional) | `.env` `TOMORROW_WEATHER_API` | Cross-check in severe weather |

Default timezone in examples is often `America/Halifax` — set `timezone` and coordinates for **your** city.

**Verify:** `/weather now` matches your area.

---

## Layer 5 — Meals (`#furnace`)

| Piece | Setup | Unlocks |
|-------|-------|---------|
| Channel ID | `furnace_channel_id` | Meal-planning mode (chef) |
| Spoonacular (optional) | `.env` `SPOON_API_KEY` | Recipe search |

**Verify:** post in `#furnace`: "What's on the meal plan this week?"

Tools: `get_meals`, `set_meal`, grocery list — see [Tools § Meals](../user-guide/tools.md).

---

## Layer 6 — Cameras (Frigate)

| Piece | Setup | Unlocks |
|-------|-------|---------|
| Frigate URL + MQTT | `frigate` block in config | Person alerts, `/snap` |
| Camera labels | `frigate.cameras`, `cameras_enabled` | Per-camera alerts |

**Verify:** `/snap <camera>` returns an image; motion alert in `#security` (if configured).

Admin: `/frigate_mode`, `/frigate_camera` — see [Slash commands](../user-guide/slash-commands.md).

---

## Layer 7 — Network (Unifi + homelab)

| Piece | Setup | Unlocks |
|-------|-------|---------|
| Unifi API key | `.env` `UNIFI_KEY` + `presence.unifi_host` | MAC presence, `/speedtest` |
| Network watchman | `network_watchman.critical_hosts` | `/network`, `get_network_status` |

**Verify:** `/speedtest` or `/network` (admin).

Without Unifi: skip network tools; Wi-Fi presence may still work via HA device trackers.

---

## Layer 8 — Gmail (Bernie's mailbox)

| Piece | Setup | Unlocks |
|-------|-------|---------|
| Gmail API enabled | Same OAuth client as calendar | — |
| Gmail token | `python scripts/auth_gmail.py` | `send_email`, inbox signals, study guide delivery |

Use a **dedicated bot mailbox** — not a personal inbox. Guide: [Google OAuth § Gmail](../google-oauth.md#4-gmail-auth-gmail_tokenjson-optional).

**Verify:** `/email` in `#anvil` (admin) or ask "anything from school in email lately?"

---

## Layer 9 — Local LLM (Ollama / LiteLLM)

| Piece | Setup | Unlocks |
|-------|-------|---------|
| Ollama on LAN | `ollama_base_url`, `ollama_models` | Background workers, chat fallback |
| LiteLLM proxy | `litellm_base_url`, `litellm_models` | `/model` switching to OpenRouter etc. |

Workers (reflection, consolidation, research) prefer local models — **$0 API cost** for overnight jobs.

**Verify:** `/model` in `#anvil` lists models; nightly jobs in logs use worker model.

---

## Layer 10 — Regional (Halifax defaults)

These ship tuned for **Halifax, Nova Scotia**. Other cities need config or code forks.

| Feature | Default source | Forking elsewhere |
|---------|----------------|-------------------|
| **Garbage / recycling** | Halifax ReCollect ICS | Find your municipality's collection calendar API or ICS URL; wire in `garbage_service.py` or config |
| **Bus tracking** | Halifax Transit GTFS-RT (`gtfs.halifax.ca`) | Point at your agency's GTFS-RT VehiclePositions feed; map HA zones for landmarks |
| **Weather** | Environment Canada + coords | Set `lat`/`lon`; EC covers Canada — others may need a different primary provider |
| **School calendar** | Your Google Calendar IDs | Any school's calendar works — not Halifax-specific |

**Verify (Halifax):** `/garbage` and `/bus help`.

For adopters outside Halifax: treat `/garbage` and `/bus` as **reference implementations** — search for "GTFS-RT vehicle positions" (transit) and "recycling collection ICS" (waste) for your region.

---

## Layer 11 — Observability (optional)

| Piece | Setup | Unlocks |
|-------|-------|---------|
| Langfuse | `.env` Langfuse keys + host | LLM traces, cost dashboards |
| Eval pipeline | `#anvil` `/eval_mode`, etc. | Shadow model comparison (operators) |

Most households skip this initially.

---

## Suggested order

```
0 Discord + Anthropic
1 Calendar
2 Family members
3 Home Assistant (if you have it)
4 Weather coords
5 Meals channel
6 Frigate (if you have cameras)
7 Unifi / network (if you care)
8 Gmail (if you want email)
9 Ollama (if you want cheap background jobs)
10 Regional features (fork if not in Halifax)
```

After each layer: one verification command, then `/reload` or container restart if you changed secrets.

Broken? → [Troubleshooting](../help/troubleshooting.md).
