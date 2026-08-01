# Test layout (family-bot-1ov.1)

**Canonical discover root:** `bot/tests/`

- All production unittests live under `bot/tests/`.
- Run: `PYTHONPATH=bot python -m unittest tests.<module>` from repo root, or
  `./scripts/run_container_unittest.sh tests.<module>` on bernie-host.
- Mapped gate list: `tests/gate_manifest.txt` (`BOT_TESTS` only; `ROOT_TESTS` empty).

This directory keeps:

- `gate_manifest.txt` — curated mapped suite
- `README.md` — this file

Do **not** add new test modules here. Put them in `bot/tests/` and append to `BOT_TESTS` in the manifest.
