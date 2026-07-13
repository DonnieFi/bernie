# Troubleshooting

Symptom → likely cause → fix. Work top to bottom within each section.

When multiple things fail, use the [Recovery order](#recovery-order) at the bottom.

---

## Recovery order

When Bernie feels broadly broken:

1. **Container health** — `docker compose ps` — all three running? cognition `healthy`?
2. **Logs** — `docker compose logs --tail=100 bernie-discord` (or cognition if writes fail)
3. **`INTERNAL_POST_SECRET`** — same value in `.env`; cognition + discord + api all load it
4. **Discord** — token valid, Message Content Intent on
5. **Config** — `config.json` exists, valid JSON, snowflakes correct
6. **Google** — `credentials/token.json` present if using calendar
7. **`/reload`** in `#anvil` after config edits — or full restart for code changes

---

## Discord

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Bot offline in member list | Bad token; container crash | Check `.env` `DISCORD_TOKEN`; `logs bernie-discord` |
| Online, ignores all messages | Message Content Intent off | Developer Portal → Bot → enable intent → restart discord container |
| Ignores one channel | Wrong channel ID; no permission | Verify `schedule_channel_id`; bot role can View/Send in channel |
| Slash commands 404 | Missing `applications.commands` on invite | Re-invite URL; restart discord |
| Replies "can't do that" for admin tools | Wrong channel or role | Use `#anvil`; check `family_members.*.role` |
| DMs don't work | User `dm_mode` off | `/dm on` or `set_dm_mode` |

Guide: [Discord onboarding](../discord-onboarding.md)

---

## Containers & writes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Tool writes fail silently | cognition down; secret mismatch | `docker compose ps`; match `INTERNAL_POST_SECRET` |
| `401` on internal API | Wrong internal secret | Regenerate secret in `.env`; restart all containers |
| SQLite locked errors | Rare multi-writer contention | Ensure only cognition writes DB; restart cognition |
| discord healthy, no replies | Anthropic key missing/invalid | Check `.env` `ANTHROPIC_API_KEY`; logs for 401 |

---

## Google Calendar

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| "Google token missing" | No `token.json` | Run `python scripts/auth_google.py` on host |
| Empty calendar list | Wrong Google account signed in | Delete `token.json`; re-auth with calendar owner account |
| `/summary` empty but Google has events | Calendar ID not in config | Paste IDs from auth script → `shared_calendars` |
| Stale events after edit in Google app | 30 min cache | Ask Bernie to call calendar tool; or wait TTL |
| `redirect_uri_mismatch` | Web OAuth client instead of Desktop | Recreate OAuth client as **Desktop app** |

Guide: [Google OAuth](../google-oauth.md)

---

## Gmail

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Email tools say not configured | No `gmail_token.json` | Run `auth_gmail.py` (optional feature) |
| Send blocked | Recipient not in `family_members` | Add email to family member entry |
| Kid send doesn't go out | Approval flow | React ✅ on `#smithy` draft |

---

## Home Assistant

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| All HA tools fail | Bad URL/token | `home_assistant.url` reachable from container; long-lived token |
| Presence always empty | Wrong `person.*` entities | Use person entities, not stale device_tracker |
| Light control fails | Entity not in registry | `/ha_entities` or `get_home_state query=` to discover ID |
| Container can't reach HA | Docker network | Use LAN IP not `localhost`; extra_hosts if needed |

---

## Weather

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Wrong city | Default coords 0,0 | Set `lat`, `lon`, `location` in config |
| Tool errors | Missing coords | Halifax example uses EC — needs valid coordinates |

---

## Frigate / cameras

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `/snap` fails | Frigate URL wrong; camera off | Check `frigate` config; container can reach Frigate host |
| No `#security` alerts | MQTT not wired; mode off | `frigate.mode`; MQTT broker reachable from cognition |
| Alerts for everything | `alert_labels` too broad | Tune labels; `frigate_set_camera` per cam |

---

## Network / Unifi

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `/speedtest` empty | UniFi key missing | `.env` `UNIFI_KEY`; `presence.unifi_host` |
| `/network` all down | Wrong critical_hosts IPs | Update `network_watchman.critical_hosts` for your LAN |

---

## Transit (Halifax)

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| No buses found | Feed down; wrong route ID | Verify `gtfs.halifax.ca` reachable; use route numbers from agency |
| `/bus near` wrong landmark | HA zone missing | Configure `zone.home` and landmarks in HA |
| Tracking never announces home | GPS/person entity stale | Check person tracker in HA |

**Other cities:** feed URL and zone setup differ — [Optional integrations](../integrations/optional-services.md).

---

## Garbage (Halifax)

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `/garbage` empty | ICS URL wrong/unreachable | Check ReCollect ICS config for your municipality |
| Wrong collection type | Parser filters | Halifax parser skips depot events — fork for your ICS format |

---

## Web dashboard

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Connection refused | api container down | `docker compose ps bernie-api`; port 8000 published |
| PIN won't accept | Wrong bcrypt hash | Regenerate hash; `reset_web_pin` |
| CORS error on custom domain | `cors_origins` unset | Add explicit origin list in config |

Guide: [Web dashboard](../user-guide/web-dashboard.md)

---

## Config & git

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `config.json` vanished after pull | File was gitignored mid-history | Restore from backup or `git show '88636f6^:config.json'` |
| `/reload` doesn't pick up changes | Syntax error in JSON | Validate JSON; check logs for parse errors |
| Behaviour file edits ignored | In-process cache | `/reload` calls cache invalidation; or restart |

---

## LLM / models

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Slow replies | Tool loop; queue depth | Normal for multi-tool turns; check `executor.max_steps` |
| Timeout errors | `llm_step_timeout_s` | Increase in config or simplify request |
| LiteLLM model fails | Proxy down; wrong model id | `/model` list; check `litellm_base_url` |
| Fallback to Ollama | Primary model error | Expected failure path — verify Ollama URL if unwanted |

---

## Performance

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| High API cost | `#slag` long sessions | Switch model; enable local workers |
| RAM spike during turn | Normal for tool loops | ~400 MB peak is typical |

---

## Still stuck?

1. Collect: `docker compose ps`, last 50 lines of discord + cognition logs (redact tokens)
2. Note: what you asked, which channel, expected vs actual
3. Open a GitHub issue — **no secrets, no family PII, no street addresses**

See also [FAQ](faq.md).
