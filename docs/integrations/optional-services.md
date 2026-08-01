# Optional integrations

What each external service unlocks, and what you can skip. Bernie runs with **Discord + Anthropic only** — everything else layers on.

See [Layer by layer](../getting-started/layer-by-layer.md) for setup order.

---

## Minimum viable Bernie

| Service | Required? | Unlocks |
|---------|-----------|---------|
| Discord | **Yes** | Chat, slash commands, DMs, notifications |
| Anthropic API | **Yes** | Replies and tool loops |
| `config.json` | **Yes** | Channel IDs, family, timezone |
| `INTERNAL_POST_SECRET` | **Yes** | Three-container write path |

**Verify:** message in main channel → reply.

---

## Integration matrix

| Integration | Config / env | Tools & features | Skip if… |
|-------------|--------------|------------------|----------|
| **Google Calendar** | `credentials/token.json`, calendar IDs | `/summary`, `/week`, events, school, homework | You only want chat |
| **Gmail** | `gmail_token.json` | Send email, inbox signals, study guide delivery | No bot mailbox needed |
| **Home Assistant** | `home_assistant.*`, presence | Lights, presence, temps, zones, snapshots | No smart home |
| **Frigate** | `frigate.*`, MQTT | `/snap`, `#security` alerts | No cameras |
| **UniFi** | `UNIFI_KEY`, `presence.unifi_*` | MAC presence, `/speedtest`, client list | No UniFi |
| **Network watchman** | `network_watchman.critical_hosts` | `/network`, homelab status | Single machine, no homelab |
| **LiteLLM** | `litellm_base_url`, `LTE_LLM_MASTER_KEY` | `/model` switching, OpenRouter models | Anthropic-only |
| **Ollama** | `ollama_base_url`, `ollama_models` | Local workers, chat fallback | Cloud-only |
| **Spoonacular** | `SPOON_API_KEY` | Recipe search in `#furnace` | Manual meal planning |
| **Tomorrow.io** | `TOMORROW_WEATHER_API` | Weather cross-check | EC-only weather fine |
| **Oura** | `OURA_TOKEN` | Oura sleep data | No Oura rings |
| **FlightAware** | `FLIGHT_AERO_KEY` | `/flight` tracking | Don't track flights |
| **SearxNG / search** | search URL in config | `web_search` | No web search |
| **Langfuse** | `LANGFUSE_*` | Traces, cost metrics | Don't need observability |
| **OpenWebUI** | `openwebui_url`, users | Separate web chat path | Discord-only |

---

## Regional features (Halifax defaults)

These work out of the box for **Halifax, Nova Scotia**. Adopters elsewhere should treat them as reference implementations.

### Garbage & recycling

| | |
|---|---|
| **What Bernie uses** | Halifax Regional Municipality ReCollect ICS calendar |
| **Tools** | `/garbage`, `get_garbage_schedule` |
| **Halifax** | Works with default parser in `garbage_service.py` |

**Your city:** Search for municipal waste "collection calendar" — many publish ICS/iCal feeds. Options:

1. Find ICS URL for your address zone → add config key (if supported in your fork)
2. Adapt `garbage_service.py` parser for your provider's format
3. Disable `/garbage` and use calendar reminders instead

No street address belongs in docs or git — configure collection zone in your private config only.

### Transit / bus tracking

| | |
|---|---|
| **What Bernie uses** | [Halifax Transit GTFS-RT](https://www.halifax.ca/transit) VehiclePositions protobuf |
| **Default feed** | `gtfs.halifax.ca/realtime/Vehicle/VehiclePositions.pb` |
| **Tools** | `/bus route|near|track|stop`, `get_route_buses`, `get_bus_proximity`, `track_vehicle` |
| **Landmarks** | Home Assistant zones (`zone.home`, school zones, etc.) — not street addresses |

**Your city:**

1. Find if your transit agency publishes **GTFS-RT VehiclePositions** (many do)
2. Point feed URL in transit config / service module
3. Define HA zones for "near home" / "near school" landmarks
4. GTFS static route IDs vary by agency — use their route numbering

Resources: [GTFS Realtime spec](https://gtfs.org/realtime/), your agency's open data portal.

### Weather

| | |
|---|---|
| **Primary** | Environment Canada via coordinates |
| **Needs** | `lat`, `lon`, `timezone` in config |
| **Halifax** | Works anywhere in Canada with correct coords |

Non-Canada: may need a different weather provider in `weather_service.py` or config.

### School calendar

Not Halifax-specific — any Google Calendar ID in `school_calendars` with a student name. Class reminders are DM-only.

---

## Cognitive workers (automatic)

No extra API keys beyond `worker_model` / Ollama:

| Worker | Schedule (typical) | Output |
|--------|-------------------|--------|
| Nightly digest | ~02:00 local | Behavioral insights |
| Reflection | ~02:15 | Tomorrow context note |
| Consolidation | ~03:15 | Recurring routines |
| Study guide | Before tagged events | DM study pack |
| Research | On request | DM research results |

Prefer local Ollama → **$0** marginal API cost for overnight jobs.

---

## Three-container stack

| Container | Role | Needs |
|-----------|------|-------|
| `bernie-discord` | Discord gateway | `DISCORD_TOKEN` |
| `bernie-api` | Dashboard + API | Same `.env` |
| `bernie-cognition` | SQLite writer, workers, BTS | Same `.env`, RW `./data` |

All three must share identical `INTERNAL_POST_SECRET`.

Public compose: `docker-compose.public.yml` — minimal host mounts.  
Homelab: `docker-compose.yml` — LAN certs, extra_hosts for LiteLLM.

---

## Cost impact by integration

| Integration | Typical API cost impact |
|-------------|-------------------------|
| Calendar prefetch | Reduces redundant tool calls (saves $) |
| `#slag` long chat | Increases turns ($) |
| Shadow eval / harness | Extra model calls ($) — off by default |
| Ollama workers | Decreases overnight API ($0) |
| Web search | Small per-query cost if via paid gateway |

Ballpark: [FAQ § cost](../help/faq.md#how-much-does-api-usage-cost)

---

## Decision guide

```
Start → Discord + Anthropic
      → Google Calendar (high value)
      → Family members + roles
      → Home Assistant (if you have it)
      → Pick any: Frigate | UniFi | Gmail | Ollama
      → Halifax-only: garbage + bus (or fork for your city)
      → Operators: Langfuse + eval
```

When in doubt, skip an integration until the previous layer verifies cleanly.
