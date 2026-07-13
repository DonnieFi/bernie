# Web dashboard

Bernie serves a family dashboard on **port 8000** from the `bernie-api` container.

```
http://<your-host>:8000
```

Same machine as Docker, or your LAN IP / reverse proxy hostname.

---

## Login

1. Open the URL in a browser
2. Pick your family avatar on the login screen
3. Enter your **PIN** if `web_pin_hash` is set in `config.json` for that person
4. Leave PIN unset for passwordless pick-your-user login (LAN trust model)

The browser title uses `family_name` from `config.json` (local installs often `"Example"`; OSS examples use a generic sample name).

Generate a PIN hash:

```bash
python3 -c "import bcrypt; print(bcrypt.hashpw(b'1234', bcrypt.gensalt()).decode())"
```

Paste into `family_members.<name>.web_pin_hash`. Admin can reset via `reset_web_pin` tool / API.

---

## Navigation (6 items)

Family users see five tabs plus **Admin** (admin/parents only). Number keys `1`–`6` match the visible sidebar order.

| Panel | Typical key | What you see |
|-------|-------------|--------------|
| **Today** | `1` | Weather, schedule, presence strip, Ask Bernie, quick actions |
| **Home** | `2` | **Home Assistant control plane** — rooms, lights, switches, climate, media, HA automations |
| **Plan** | `3` | Calm chores / kanban (HUD mode admin-only) |
| **People** | `4` | Presence, maps, memory |
| **Security** | `5` | Frigate cameras and events |
| **Admin** | `6` | Usage, Network, Models, Config (personality docs: soul/bernie/people/…), Logs, System |

### Home (Home Assistant)

Home is Bernie’s product spine: your HA instance is the map for lights, switches, climate, media, and automations.

| Endpoint | Role |
|----------|------|
| `GET /api/home/dashboard` | Combined rooms + switches + media + climate + automations + system + temps |
| `GET /api/rooms` | Floor/room light map (live HA) |
| `POST /api/lights/{id}` | On/off, brightness, color temp, RGB |
| `GET/POST /api/switches/{id}` | Switch state |
| `GET/POST /api/media/{id}` | Media players |
| `GET /api/climate` | Climate / air quality sensors |
| `GET/POST /api/ha/automations…` | HA automations (not Bernie CRUD automations) |
| `GET /api/system` | HA system entities / update checks |

If Home is empty, configure `home_assistant` in `config.json` (URL, long-lived token, entities). The dashboard shows an error banner when HA is unreachable.

**Admin › Usage** rehosts the former Activity dashboard (token/cost view). Phase 42 may replace this with a Langfuse-native usage rewrite.

**Chat @ Bernie** (OpenWebUI) is linked from **Admin**, not the main sidebar. External agents can use the OpenAI-compatible API on `:8000` (see the project **README → API** section). Interactive REST docs (Swagger) live at `http://<host>:8000/docs` (also `/redoc`, `/openapi.json`).

Removed from the **family nav** only (not from the HTTP API): Nano chat chrome, Cognition **panel**, and the old 13-item operator sprawl. Operator/cognition **API routes stay** for scripts and OSS integrators.

---

## Plan board

Unified Kanban for chores and agent jobs:

| Lane | Meaning |
|------|---------|
| todo | Not started |
| ready | Ready to pick up |
| running | In progress (agent or person) |
| blocked | Needs help |
| done | Complete |

Task types in the API: `chore`, `research`, `bernie`, `code`, `system`. The family **Plan** UI shows `chore` / `research` / `bernie` in calm mode; `code` and HUD are admin-only surfaces.

Same data as Discord task commands — pick whichever surface fits.

---

## CORS and reverse proxy

- **Direct access** (`:8000` on same host): default CORS is fine
- **Custom hostname** (e.g. `https://bernie.lan`): set `cors_origins` in `config.json`

Never set `cors_origins` to `"*"` in production — refused at runtime.

See [Deploy guide § CORS](../deploy.md).

---

## Who should expose this?

| Exposure | Recommendation |
|----------|----------------|
| LAN only | Default — fine for most families |
| VPN (Tailscale, etc.) | Good for phone access away from home |
| Public internet | **Not recommended** without auth hardening + TLS |

Treat **Admin** like operator access — config and logs live there.

---

## Relationship to Discord

| Surface | Best for |
|---------|----------|
| Discord | Daily chat, alerts, slash commands, kids |
| Dashboard | Visual schedule, Plan, cameras, operator review |

Bernie does not require the dashboard to run — Discord-only is valid.

---

## Troubleshooting

| Issue | Check |
|-------|-------|
| Blank page | `docker compose logs bernie-api`; port 8000 reachable |
| Login fails | PIN hash correct; user in `family_members` |
| No camera tiles | Frigate URL in config; network from container |
| Tasks empty | Same SQLite DB mounted — cognition container healthy |
| Kids see Admin | Role should be `family` / `kids` — Admin is hidden |

More: [Troubleshooting](../help/troubleshooting.md).
