# Discord onboarding

Step-by-step setup so Bernie can read family chat, run slash commands, and route admin tools to the right channels.

Pair with the **three-container** stack (`docker compose -f docker-compose.public.yml up`).

Channel names in this repo use forge metaphors (`#smithy`, `#anvil`, …). Use neutral names if you prefer — only the **numeric channel IDs** in `config.json` matter.

| Forge name | Neutral | Role |
|------------|---------|------|
| `#smithy` | `#main` | Family chat — summaries, presence, alerts |
| `#anvil` | `#admin` | Model switch, `/reload`, eval (parents/admin only) |
| `#furnace` | `#meals` | Meal planning |
| `#bellows` | `#chatter` | Optional — bot stays silent |
| `#slag` | `#ai-chat` | Extended AI chat |
| `#security` | `#security` | Frigate / camera alerts |

---

## Checklist overview

- [ ] Discord application + bot token → `.env`
- [ ] Privileged intents enabled
- [ ] Bot invited to your server with slash-command scope
- [ ] Server + channel + user snowflakes → `config.json`
- [ ] `INTERNAL_POST_SECRET` in `.env` (same value for all containers)
- [ ] `docker compose up` → test message in main channel

---

## 1. Create the application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications).
2. **New Application** → name it (e.g. *Bernie*).
3. **Bot** tab → **Add Bot** → **Reset Token** → copy once → `.env`:

   ```bash
   DISCORD_TOKEN=your_bot_token_here
   ```

4. Under **Privileged Gateway Intents**, enable:
   - **Message Content Intent** — required for Bernie to read normal messages
   - **Server Members Intent** — if you use role-based RBAC from Discord roles
   - **Presence Intent** — only if you rely on Discord presence (HA presence is more common)

5. Upload an avatar (optional) — use `web/static/bernie-avatar.png` (heraldic gryphon crest).

---

## 2. Invite the bot to your server

1. **OAuth2 → URL Generator**.
2. Scopes: **`bot`** and **`applications.commands`** (slash commands).
3. Bot permissions (minimum useful set):

   | Permission | Why |
   |------------|-----|
   | View Channels | See family channels |
   | Send Messages | Reply in chat |
   | Embed Links | Rich summaries |
   | Attach Files | Frigate snapshots |
   | Read Message History | Context for replies |
   | Add Reactions | Optional UX |
   | Use Application Commands | `/weather`, `/summary`, etc. |

4. Copy the generated URL → open in browser → pick your **family server** → authorize.

The bot should appear offline until containers start.

---

## 3. Copy IDs into `config.json`

1. Discord → **User Settings → Advanced → Developer Mode** → On.
2. Right-click your **server** → **Copy Server ID** → `guild_id`.
3. Create channels (see table above), then right-click each → **Copy Channel ID**:

   | Config key | Typical channel |
   |------------|-----------------|
   | `schedule_channel_id` | Main / `#smithy` |
   | `anvil_channel_id` | Admin / `#anvil` |
   | `furnace_channel_id` | Meals / `#furnace` |
   | `slag_channel_id` | AI chat / `#slag` |
   | `security_channel_id` | Security / `#security` |
   | `bellows_channel_id` | Chatter / `#bellows` (optional) |

4. Right-click each family member → **Copy User ID** → `family_members.*.discord_id`.
5. Set `admin_discord_id` to whoever runs `/model`, `/reload`, etc.

**Hour-1 template:** [`config.minimal.example.json`](../config.minimal.example.json)  
**Full reference:** [`config.example.json`](../config.example.json)

```bash
cp config.minimal.example.json config.json
$EDITOR config.json   # snowflakes, timezone, family_members
```

---

## 4. Environment + secrets

```bash
cp .env.example .env
```

| Variable | Required | Where to get it |
|----------|----------|-----------------|
| `DISCORD_TOKEN` | Yes | Developer Portal → Bot → Token |
| `ANTHROPIC_API_KEY` | Yes | [console.anthropic.com](https://console.anthropic.com) |
| `INTERNAL_POST_SECRET` | Yes | Generate: `openssl rand -hex 32` — **same** in every container |

Optional: Google paths (defaults work with `./credentials` mount) — see [google-oauth.md](google-oauth.md).

---

## 5. First run

```bash
# Optional but recommended for calendar
python scripts/auth_google.py

docker compose -f docker-compose.public.yml up -d --build
docker compose -f docker-compose.public.yml logs -f bernie-discord
```

Homelab hosts with LAN certs / extra mounts may use `docker compose up` (`docker-compose.yml`) instead.

**Smoke test:** send a short message in the main channel. Bernie should reply within a few seconds.

**Slash test:** `/weather now` or `/summary` (after calendar auth).

**Dashboard (optional):** `http://<host>:8000`

---

## 6. Common failures

| Symptom | Check |
|---------|--------|
| Bot offline in member list | Wrong `DISCORD_TOKEN`; container crash — `logs bernie-discord` |
| Bot online but ignores messages | **Message Content Intent** off in Developer Portal |
| Slash commands missing | Re-invite with `applications.commands`; restart `bernie-discord` |
| `401` / write errors | `INTERNAL_POST_SECRET` must match across containers; `bernie-cognition` healthy |
| Replies in wrong channel | Channel snowflakes in `config.json`; bot has access to that channel |
| Admin tools in family channel | `anvil_channel_id` must point at admin-only channel |

---

## 7. Security

- Treat `DISCORD_TOKEN` like a password — rotate if leaked (Developer Portal → Reset Token).
- Keep `#anvil` restricted to parents/admins in Discord channel permissions.
- Never commit real snowflakes or tokens — use `YOUR_*` placeholders in git.
- Prefer a private family server; don't invite untrusted users to admin channels.

---

## Next steps

- [Google OAuth (Calendar + Gmail)](google-oauth.md)
- [Deploy guide](deploy.md) — HA, Frigate, LiteLLM, backups
- [Family members & RBAC](family.md) — calendars, device trackers, roles
