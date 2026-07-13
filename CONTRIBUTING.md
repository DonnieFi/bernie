# Contributing to Bernie

Thanks for interest in Bernie. This project is a **family-shaped, fork-friendly** home bot — not a multi-tenant SaaS product.

## Where to talk

| Topic | Channel |
|-------|---------|
| Bugs / feature requests | GitHub **Issues** |
| Setup / “how do I…?” | GitHub **Discussions** (when enabled) |
| Security vulnerabilities | See [SECURITY.md](SECURITY.md) — **not** public issues |

## Layout (quick)

| Path | Role |
|------|------|
| `bot/` | Runtime app (Docker entrypoint) |
| `web/` | Dashboard UI |
| `scripts/` | Operator helpers — [scripts/README.md](scripts/README.md) |
| `docs/` | Guides (optional MkDocs site) |

## Development setup (short)

1. Copy env and config:
   ```bash
   cp .env.example .env
   cp config.minimal.example.json config.json   # hour-1
   # or: cp config.example.json config.json     # full-shape reference
   ```
2. Fill Discord token + model keys in `.env` / `config.json`.
3. Three-container stack (public/generic first run):
   ```bash
   docker compose -f docker-compose.public.yml up -d --build
   ```
   Homelab hosts with LAN certs / LiteLLM overrides may use `docker-compose.yml`
   (+ optional compose overlays) instead.
4. Discord setup: [docs/discord-onboarding.md](docs/discord-onboarding.md).

## Tests

Canonical path on a machine with Docker (`bernie-api` image):

```bash
./scripts/run_container_unittest.sh tests.test_eval_policy
./scripts/run_mapped_suite.sh   # full gate (one agent; uses flock)
```

New tests go under `bot/tests/`. See project testing docs if present in your tree.

## Docs site (optional)

```bash
pip install -r requirements-docs.txt
mkdocs serve
```  
Use **unittest** only (not pytest).

## Pull requests

- Branch from the active integration branch / `main` as documented in the issue.
- Prefer small, reviewable diffs. Do not commit secrets, real Discord IDs, family docs, or LAN credentials.
- Keep behavior changes covered by a named unittest module when practical.

## Code of conduct

Be respectful. This is a household project shared for others to run their own instance — not a platform for spam or abuse.
