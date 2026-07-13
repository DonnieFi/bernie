# Quickstart

Install Bernie, connect Discord, and get your first reply in about **30–45 minutes**. You can add Google Calendar, Home Assistant, and the rest later — see [Layer by layer](layer-by-layer.md).

---

## Pick your path

| Goal | Do this first | Skip for now |
|------|---------------|--------------|
| **Discord chat only** | Steps 1–4 below | Calendar, HA, Frigate |
| **Schedule + summaries** | Steps 1–5 (add Google OAuth) | HA, cameras, email |
| **Full household bot** | Quickstart, then [Layer by layer](layer-by-layer.md) | — |

**Rule:** get one clean reply in your main channel before enabling extra integrations. If chat works, everything else is configuration.

---

## What you need

| Requirement | Notes |
|-------------|-------|
| Docker + Compose v2.20+ | Runs three containers: discord, api, cognition |
| Discord server | Private family server recommended |
| Anthropic API key | [console.anthropic.com](https://console.anthropic.com) |
| Python 3.11+ (host) | Only for one-time Google OAuth scripts — optional at first |

---

## Step 1 — Clone and secrets

```bash
git clone https://github.com/DonnieFi/bernie.git family-bot
cd family-bot

cp .env.example .env
openssl rand -hex 32   # paste into INTERNAL_POST_SECRET in .env
```

Edit `.env` — minimum required:

```bash
DISCORD_TOKEN=…           # Developer Portal → Bot → Token
ANTHROPIC_API_KEY=sk-ant-…
INTERNAL_POST_SECRET=…    # same value for all containers
```

Full variable list: [Environment reference](../reference/environment.md).

---

## Step 2 — Discord

Follow **[Discord onboarding](../discord-onboarding.md)**:

1. Create bot application + enable **Message Content Intent**
2. Invite with `bot` + `applications.commands` scopes
3. Copy server, channel, and user snowflakes into config

```bash
cp config.minimal.example.json config.json
$EDITOR config.json
```

Required keys for hour-one: `guild_id`, `schedule_channel_id`, `anvil_channel_id`, `admin_discord_id`, `family_members`, `timezone`.

---

## Step 3 — Start containers

```bash
docker compose -f docker-compose.public.yml up -d --build
docker compose -f docker-compose.public.yml logs -f bernie-discord
```

**Success looks like:** logs show Discord connected; no crash loop on `bernie-cognition`.

Homelab hosts with LAN certs: use `docker compose up` instead — see [Deploy guide](../deploy.md).

---

## Step 4 — First reply

1. Open your **main family channel** (config: `schedule_channel_id`)
2. Send: `Hey Bernie, what time is it?`
3. Bernie should reply within a few seconds

**If nothing happens:** [Troubleshooting § Discord](../help/troubleshooting.md#discord).

---

## Step 5 — Calendar (recommended)

Without calendar auth, schedule tools and `/summary` will fail gracefully.

```bash
pip install --user google-auth-oauthlib google-api-python-client
python scripts/auth_google.py
```

Paste calendar IDs from the script output into `config.json` → `shared_calendars` and `family_members.*.calendars`.

Full walkthrough: **[Google OAuth](../google-oauth.md)**.

Then:

```bash
# In Discord #anvil (admin channel)
/reload
```

Test: `/summary` or `/weather now`

---

## Step 6 — Try these

Once chat works, natural language is fine — Bernie routes to tools automatically.

| Say or type | What happens |
|-------------|--------------|
| `What's the weather?` | Live weather for your configured location |
| `/summary` | Today's calendar highlights in channel |
| `Who's home?` | Presence (needs Home Assistant — optional) |
| `What's for dinner this week?` | Meal plan (best in `#furnace`) |

More examples: [What you can ask](../user-guide/what-you-can-ask.md).

---

## What to do next

| Next step | Guide |
|-----------|-------|
| Add HA, Frigate, Gmail, Ollama one at a time | [Layer by layer](layer-by-layer.md) |
| See every slash command | [Slash commands](../user-guide/slash-commands.md) |
| See every tool | [Tools](../user-guide/tools.md) |
| Understand channels and modes | [Channels & modes](../user-guide/channels-and-modes.md) |
| Open web dashboard | [Web dashboard](../user-guide/web-dashboard.md) — `http://<host>:8000` |

---

## Backup before `git pull`

`config.json`, `.env`, and `credentials/` are **gitignored**. They stay on disk but won't come from git. Back them up outside the repo before major pulls:

```bash
cp config.json config.json.bak-$(date +%Y%m%d)
cp -a credentials credentials.bak-$(date +%Y%m%d) 2>/dev/null || true
```

See [FAQ § git pull](../help/faq.md#git-pull-removed-my-config).
