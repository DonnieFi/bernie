# Architecture documentation

Reading order for understanding Bernie internals (public maps of how the system works).

## Start here

1. [Project README](../../README.md) — what Bernie is, how to run it
2. [DECISIONS.md](./DECISIONS.md) — current cross-cutting decisions (SQLite, queues, when not to use Redis, etc.)
3. [EVENT_BUS_FLOW.md](./EVENT_BUS_FLOW.md) — real queues, producers, consumers (no external broker)
4. [REQUEST_LIFECYCLE.md](./REQUEST_LIFECYCLE.md) — Discord/HTTP → tools → workers
5. [BERNIE_SYSTEM_MAP.md](./BERNIE_SYSTEM_MAP.md) — components, data stores, boundaries

## Related user docs

- [DB schema](../db-schema.md) — tables
- [Deploy](../deploy.md) — running the stack
- [Capabilities](../capabilities.md) — user-facing feature matrix

## Notes

- **ADRs** (numbered decision records under `docs/adr/`) stay on the private monorepo only — historical debate, not required to run Bernie.
- Config shape: `config.example.json` (committed) vs private `config.json` (gitignored).
- Prefer updating these maps when behavior changes so newcomers can trust them.
