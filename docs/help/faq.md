# FAQ

Common questions about running Bernie at home.

---

## General

### What is Bernie?

A self-hosted Discord bot for household coordination — calendar, presence, smart home, meals, tasks, cameras, and optional email. Chat uses **your** configured LLM (Claude, GPT, Grok, OpenRouter, Ollama, …) plus tools. Background jobs often run on local Ollama.

### Do I need Home Assistant?

No. Minimum viable: Discord + an LLM endpoint + `config.json`. HA unlocks presence, lights, transit landmarks, and snapshots.

### Is Bernie a generic ChatGPT wrapper?

No. He loads household context (calendar prefetch, modes, RBAC, scheduled summaries) and routes tools through a single audited gateway. Opinionated defaults for a family Discord layout.

### Which models / providers work?

Bring your favourite. Common paths:

- **Anthropic** direct (`claude-*` + `ANTHROPIC_API_KEY`)
- **OpenRouter / OpenAI-compatible** via LiteLLM (`or-*`, `litellm_base_url`)
- **xAI Grok** and other cloud models the same way when routed through your proxy
- **Local Ollama** for chat fallback and overnight workers

Switch live with `/model` in `#anvil`. See `active_model`, `litellm_base_url`, `ollama_base_url` in config.

---

## Cost

### How much does API usage cost?

Depends on model and chatter volume. Ballpark for a family of four on a mid-tier cloud chat model: **~$25–30/month** with normal daily use; quiet days can be **$0.02–0.05**. Scales with conversation volume and tool-loop depth, not feature count. Local-only setups can be ~$0 API.

### How do I reduce cost?

- Switch model via `/model` to a cheaper option
- Run background workers on local Ollama (default intent)
- Use `#bellows` for bot-free chat (no API)
- Limit `#slag` extended AI sessions

---

## Privacy

### What leaves my network?

- Chat text → your configured LLM provider per turn (Anthropic, OpenAI, OpenRouter, Ollama, …)
- Calendar → Google (your OAuth)
- Optional → Langfuse if enabled
- HA, Frigate, SQLite, credentials → **stay local**

See [Security & roles](../user-guide/security-and-roles.md).

### Does my LLM provider train on my data?

Depends on the vendor and tier. Check that provider’s current API/data policy (Anthropic, OpenAI, OpenRouter, xAI, …). Local Ollama stays on your hardware.

---

## Setup

### git pull removed my config

`config.json` is gitignored. An old tracked copy may disappear on pull. Recovery:

```bash
git show '88636f6^:config.json' > config.json.recovered
# merge with your off-git backup, then:
mv config.json.recovered config.json
```

Always keep dated backups **outside git**. See [Deploy guide](../deploy.md).

### Do I use the maintainer's Google OAuth?

**No.** Each household creates their own Google Cloud Desktop OAuth client. See [Google OAuth](../google-oauth.md).

### config.example vs config.minimal?

- **minimal** — hour-one Discord + one LLM path
- **example** — full shape with optional blocks (Frigate, eval, network watchman, …)

### Person context files not found?

`read_person_context` looks for `docs/{canonical_id}.md`. Match `family_members.*.canonical_id` to filenames (`dad.md`, `parent_one.md`, etc.). Generic pack: `docs/family/` copied to `docs/dad.md` on export.

---

## Discord

### Bot online but ignores messages

Enable **Message Content Intent** in Discord Developer Portal → Bot → Privileged Gateway Intents.

### Slash commands missing

Re-invite with `applications.commands` scope. Restart `bernie-discord`.

### Bernie replies in wrong channel

Check channel snowflakes in `config.json`. Bot must have access to that channel.

More: [Discord onboarding](../discord-onboarding.md), [Troubleshooting](troubleshooting.md).

---

## Features

### What runs at night?

| Time (local, typical) | Job | Output |
|-----------------------|-----|--------|
| ~02:00 | Nightly digest | `family_insights` |
| ~02:15 | Reflection worker | `tomorrow_context` |
| ~03:15 | Memory consolidation | `routines` |
| ~03:45 | SQLite backup | `data/backups/` |

You don't invoke these from chat — they run on schedule in `bernie-cognition`.

### Does Bernie work outside Halifax?

Yes for core features. **Halifax-specific defaults:**

- **Garbage** — ReCollect ICS for Halifax Regional Municipality
- **Bus tracking** — Halifax Transit GTFS-RT
- **Weather** — Environment Canada (works across Canada with correct coords)

Fork or reconfigure for other cities: [Optional integrations](../integrations/optional-services.md).

### Can kids use Bernie?

Yes — set `role: "kids"`. They get calendar, weather, own tasks, grocery; blocked from admin tools and some writes. Email send requires parent approval.

### What's the web dashboard for?

Visual Today/Tasks/Cameras — optional. Discord is the primary UI. See [Web dashboard](../user-guide/web-dashboard.md).

---

## Operations

### How do I update Bernie?

```bash
git pull
docker compose -f docker-compose.public.yml up -d --build
```

Backup config + credentials first.

### `/reload` vs restart?

`/reload` picks up `config.json` changes and refreshes behaviour file cache. Required after editing `capabilities_index.md` on disk. Container restart needed for code/image changes.

### Where are logs?

```bash
docker compose logs -f bernie-discord
docker compose logs -f bernie-cognition
```

Host bind-mount may also have `data/bot.log`.

---

## Troubleshooting pointer

Symptom → fix tables: [Troubleshooting](troubleshooting.md)

Recovery order when "something feels broken":

1. `docker compose ps` — all three healthy?
2. Logs on crashed container
3. `INTERNAL_POST_SECRET` identical in `.env`
4. Discord intents + token
5. Google tokens if calendar broken
6. `/reload` in `#anvil`

---

## Contributing & license

MIT — [LICENSE](../../LICENSE). Bugs: GitHub issues without secrets/PII. [CONTRIBUTING](../../CONTRIBUTING.md).
