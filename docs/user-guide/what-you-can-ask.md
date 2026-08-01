# What you can ask

Bernie understands natural language — you don't need slash commands. These examples show what works in each area. Replace names with your family members.

**Halifax-specific:** garbage and bus examples assume Halifax defaults. Elsewhere, swap in your local waste calendar and transit agency — see [Optional integrations](../integrations/optional-services.md).

---

## Calendar & schedule

| You might say | Bernie will… |
|---------------|--------------|
| "What's on today?" | Fetch today's events and summarize |
| "Anything this week I'm forgetting?" | Scan week + context |
| "When was the last dentist appointment?" | Search historical calendar |
| "Add soccer practice Thursday at 4pm" | Confirm details, then `create_event` |
| "Am I free Saturday afternoon?" | Check conflicts |
| "Who's coming to the BBQ?" | `get_rsvps` |

Slash shortcuts: `/summary`, `/week`, `/addevent`, `/rsvps`

---

## Weather

| You might say | Bernie will… |
|---------------|--------------|
| "Do I need a jacket?" | Live weather — never guesses |
| "Is it raining?" | Current conditions |
| "Weather this week" | Extended forecast |

Slash: `/weather now`, `/weather today`, `/weather week`

Set `lat`/`lon` in config for your city. Halifax deploys often use Environment Canada via local coordinates.

---

## School (student calendar)

| You might say | Bernie will… |
|---------------|--------------|
| "What classes do I have today?" | School schedule |
| "Any homework due this week?" | Assignments from calendar keywords |
| "When's the next test?" | Scan school calendar |

Slash: `/school`, `/homework today|tomorrow|week`

Class-period reminders go to **DMs only** — not the main channel. Hide classes in summer: `/school_schedule off`.

---

## Presence & location

| You might say | Bernie will… |
|---------------|--------------|
| "Who's home?" | `who_is_home` via HA |
| "Where's Alex?" | GPS / map link if available |
| "Is anyone still out?" | Presence summary |

Requires Home Assistant person/device trackers in config.

---

## Smart home

| You might say | Bernie will… |
|---------------|--------------|
| "Turn off the kitchen light" | Resolve entity, confirm if shared space |
| "What's the thermostat at?" | Sensor read via HA |
| "List my lights" | Device discovery |

Use friendly names — Bernie maps them via HA. High-impact actions (whole-house) may ask to confirm first.

Slash: `/temps`, `/ha_entities`

---

## Meals & groceries (`#furnace`)

| You might say | Bernie will… |
|---------------|--------------|
| "What's for dinner Friday?" | Meal plan lookup |
| "Let's do tacos Tuesday" | Add to plan |
| "Add milk to groceries" | Grocery list |
| "What did we eat last week?" | Past meal history |

Best in `#furnace` — chef mode focuses on food.

---

## Tasks & chores

| You might say | Bernie will… |
|---------------|--------------|
| "Add a chore for Jamie: clean room" | Create task |
| "What tasks are open?" | List board |
| "Mark task 12 done" | Complete (may need parent approval for kids) |
| "Remind me every Sunday to take out bins" | Create automation |

Slash: `/task_add`, `/task_list`, `/task_done`, `/automation_add`

Web UI: Tasks panel (keyboard **6**) — Kanban board.

---

## Cameras & security

| You might say | Bernie will… |
|---------------|--------------|
| "Show me the front door camera" | Frigate snapshot |
| (Automatic) Person at door | Alert in `#security` when Frigate + MQTT configured |

Slash: `/snap`, `/frigate_mode`, `/frigate_camera`

---

## Transit (Halifax example)

| You might say | Bernie will… |
|---------------|--------------|
| "Any buses on route 10 near home?" | Live GPS + HA zone landmark |
| "Track the bus until I'm home" | Start tracking session |
| "Stop tracking" | End session |

Slash: `/bus route`, `/bus near`, `/bus track`, `/bus stop`

**Not in Halifax?** Your agency may publish GTFS-RT — you'll need to point Bernie at that feed and define zone landmarks. See [Layer by layer § Regional](../getting-started/layer-by-layer.md#layer-10-regional-halifax-defaults).

---

## Garbage (Halifax example)

| You might say | Bernie will… |
|---------------|--------------|
| "Is garbage tomorrow?" | ReCollect ICS schedule |
| "When's recycling?" | Next collection dates |

Slash: `/garbage`

**Other municipalities:** find your city's collection calendar (often ICS or iCal). Wire the URL in config or adapt `garbage_service.py` for your region.

---

## Flights

| You might say | Bernie will… |
|---------------|--------------|
| "Where's flight AC123?" | FlightAware status + map when airborne |

Slash: `/flight AC123`

Requires `.env` `FLIGHT_AERO_KEY` (FlightAware Personal tier).

---

## Email (when Gmail configured)

| You might say | Bernie will… |
|---------------|--------------|
| "Anything from school this week?" | Email signal digest |
| "Read that field trip email" | Full body (parents only) |
| "Email Morgan about pickup" | Send (policy: family addresses only) |

Kids' outbound email posts to main channel for parent ✅/❌ approval.

---

## Research & deep dives

| You might say | Bernie will… |
|---------------|--------------|
| "Research summer camps in Nova Scotia" | Queue background research worker |
| "Look up whether X is open today" | Quick `web_search` |

Multi-source deep research uses `request_research` + DM when done. Quick facts use web search.

---

## Memory & preferences

| You might say | Bernie will… |
|---------------|--------------|
| "Remember we prefer oat milk" | Append to family/person context (parents) |
| "What do you know about our routines?" | Read context files + DB insights |

Human-edited `USER_OVERRIDE.md` always wins over agent-written context.

---

## Admin (`#anvil`)

| You might say | Bernie will… |
|---------------|--------------|
| "Switch model to haiku" | `litellm_switch_model` / `/model` |
| "Reload config" | Pick up config.json changes |
| "Show eval status" | Shadow pipeline stats |
| "Check container logs" | Docker log tail |

Admin tools are blocked in family channels — use `#anvil` or DMs if your role allows.

---

## Things Bernie won't guess

Always calls a tool for:

- Live weather, presence, vehicle battery, sleep scores
- Calendar state ("is today clear?")
- Network/device status
- Bus positions

If a tool returns nothing, Bernie says so — he won't invent numbers.

---

## Discovery

| You might say | Bernie will… |
|---------------|--------------|
| "What slash commands do you have?" | `list_slash_commands` |
| "Can you control the thermostat?" | `search_tools` |
| "What mode are you in?" | `describe_modes` |

Full catalog: [Tools reference](tools.md).
