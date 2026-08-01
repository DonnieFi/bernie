# Bernie documentation

Docs for installing, configuring, and using Bernie at home. Start with **Getting started**, then add integrations as you need them.

**Prefer a website?** From the repo root:

```bash
pip install -r requirements-docs.txt
mkdocs serve   # http://127.0.0.1:8001  (see mkdocs.yml)
```

---

## New here

| Guide | What you'll have when done |
|-------|----------------------------|
| [**Quickstart**](getting-started/quickstart.md) | Bernie replying in Discord in ~30–45 minutes |
| [**Layer by layer**](getting-started/layer-by-layer.md) | Calendar, HA, cameras, email — added one at a time |
| [**Discord setup**](discord-onboarding.md) | Bot token, intents, channel IDs |
| [**Google OAuth**](google-oauth.md) | Calendar + optional Gmail mailbox |
| [**OpenAI models and Codex OAuth**](subscription-providers.md) | Low-cost Luna routing or optional Codex device login |

---

## Using Bernie

| Guide | What's inside |
|-------|---------------|
| [**What you can ask**](user-guide/what-you-can-ask.md) | Natural-language examples by topic |
| [**Slash commands**](user-guide/slash-commands.md) | Every `/command` — family vs admin |
| [**Tools reference**](user-guide/tools.md) | Full tool catalog by domain |
| [**Channels & modes**](user-guide/channels-and-modes.md) | `#smithy`, `#furnace`, `#anvil`, DMs, modes |
| [**Web dashboard**](user-guide/web-dashboard.md) | Today, Home, Plan, People, Security, Admin |
| [**Security & roles**](user-guide/security-and-roles.md) | Who can do what; what leaves your LAN |

---

## Reference

| Guide | What's inside |
|-------|---------------|
| [**config.json**](reference/config.md) | Field glossary by section |
| [**.env**](reference/environment.md) | Environment variables |
| [**Optional integrations**](integrations/optional-services.md) | What each service unlocks |
| [**Deploy guide**](deploy.md) | Docker, backups, homelab topology |
| [**Family members**](family.md) | RBAC, Discord IDs, device trackers |
| [**DB schema**](db-schema.md) | SQLite tables (operators) |

---

## When something breaks

| Guide | Use when |
|-------|----------|
| [**Troubleshooting**](help/troubleshooting.md) | Symptom → fix table + recovery order |
| [**FAQ**](help/faq.md) | Cost, privacy, git pull, optional features |

---

## How Bernie is built (optional)

Curious how Discord, the API, and workers fit together? Start with [**Architecture**](architecture/README.md).

## Operator notes (advanced)

Loaded by Bernie at runtime or used in development — not required for first-time setup:

- [`capabilities.md`](capabilities.md) — behavioral rules for the model
- [`capabilities_index.md`](capabilities_index.md) — compact routing index

---

## Docs by goal

| I want to… | Read |
|------------|------|
| Get Bernie talking today | [Quickstart](getting-started/quickstart.md) |
| Wire up Google Calendar | [Google OAuth](google-oauth.md) |
| See everything Bernie can do | [Tools](user-guide/tools.md) + [Slash commands](user-guide/slash-commands.md) |
| Know minimum vs full stack | [Optional integrations](integrations/optional-services.md) |
| Fork transit or garbage for my city | [Optional integrations](integrations/optional-services.md) § Regional |
| Fix a broken install | [Troubleshooting](help/troubleshooting.md) |
