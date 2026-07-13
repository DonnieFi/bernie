# Environment variables

Secrets and paths injected via `.env` (loaded by Docker Compose `env_file`). **Never commit `.env`.**

Copy from `.env.example`:

```bash
cp .env.example .env
```

Paths below show **container paths** — on the host, files live under `./credentials/` bind-mounted to `/credentials`.

---

## Required

| Variable | Description |
|----------|-------------|
| `DISCORD_TOKEN` | Bot token from [Discord Developer Portal](https://discord.com/developers/applications) |
| `ANTHROPIC_API_KEY` | Common choice for chat (or use OpenRouter/LiteLLM/Ollama — see config) |
| `INTERNAL_POST_SECRET` | Shared secret for cross-container writes — generate with `openssl rand -hex 32`; **must match** across all containers |

Missing `INTERNAL_POST_SECRET` → write path fails closed.

---

## Google OAuth

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GOOGLE_CREDENTIALS_FILE` | For calendar | `/credentials/credentials.json` | OAuth Desktop client JSON |
| `GOOGLE_TOKEN_FILE` | For calendar | `/credentials/token.json` | From `auth_google.py` |
| `GMAIL_TOKEN_FILE` | Optional | `/credentials/gmail_token.json` | From `auth_gmail.py` |

Setup: [Google OAuth](../google-oauth.md)

Host-side auth:

```bash
export CREDENTIALS_DIR="$(pwd)/credentials"   # optional override
python scripts/auth_google.py
# Port already used?  GOOGLE_OAUTH_PORT=9090 python scripts/auth_google.py
```

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_OAUTH_PORT` | `8085` | Local browser redirect for Calendar OAuth (`auth_google.py`) |
| `GMAIL_OAUTH_PORT` | `8086` | Local browser redirect for Gmail OAuth (`auth_gmail.py`) |
| `PORT` | (see above) | Fallback if the specific `*_OAUTH_PORT` var is unset |

---

## Optional — LLM & tools

| Variable | Description |
|----------|-------------|
| `LTE_LLM_MASTER_KEY` | LiteLLM proxy admin key (if using LiteLLM) |
| `SPOON_API_KEY` | Spoonacular — meal recipe search |
| `TOMORROW_WEATHER_API` | Tomorrow.io weather cross-check |
| `OURA_TOKEN` | Oura Ring personal access token |
| `FLIGHT_AERO_KEY` | FlightAware AeroAPI — `/flight` tool |
| `UNIFI_KEY` | UniFi Network API key — presence, speedtest |

---

## Optional — Observability

| Variable | Description |
|----------|-------------|
| `LANGFUSE_PUBLIC_KEY` | Langfuse public key |
| `LANGFUSE_SECRET_KEY` | Langfuse secret key |
| `LANGFUSE_HOST` | Self-hosted Langfuse URL, e.g. `http://langfuse.lan:3000` |

When unset, tracing is disabled — Bernie runs fine without Langfuse.

---

## Container runtime

| Variable | Description |
|----------|-------------|
| `TZ` | Container timezone — compose default often `America/Halifax`; set to your zone |
| `CONFIG_PATH` | Override config file path (advanced) |
| `CREDENTIALS_DIR` | Host path for auth scripts only |
| `GMAIL_TOKEN_FILE` | Override Gmail token path |

Compose sets `ROLE`, `LOG_PREFIX`, `PYTHONUNBUFFERED` per service — usually no manual edit.

---

## Injected at runtime (not in .env)

These are read from config or fixed paths — listed for completeness:

| Item | Source |
|------|--------|
| HA token | `config.json` → `home_assistant.token` |
| Discord snowflakes | `config.json` |
| SQLite DB | `./data/family_bot.db` volume |

`config.py` also injects `bernie_api_token`, `unifi_key`, `gmail_token_file` from env at load — stripped before config writes to disk.

---

## Security notes

- Rotate `DISCORD_TOKEN` if leaked (Developer Portal → Reset Token)
- Treat `INTERNAL_POST_SECRET` like a password
- Do not paste `.env` in GitHub issues
- `.env.example` has placeholders only — no real secrets

---

## Checklist

```bash
# Minimum viable
DISCORD_TOKEN=✓
ANTHROPIC_API_KEY=✓
INTERNAL_POST_SECRET=✓

# Schedule
GOOGLE_CREDENTIALS_FILE=✓
GOOGLE_TOKEN_FILE=✓

# Optional layers — add as needed
GMAIL_TOKEN_FILE
UNIFI_KEY
SPOON_API_KEY
FLIGHT_AERO_KEY
LANGFUSE_*
LTE_LLM_MASTER_KEY
```

See [Optional integrations](../integrations/optional-services.md) for what each unlocks.
