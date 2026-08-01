"""One-shot: run nightly_eval_worker for yesterday from inside the container.

Caveat: this reads /app/config.json directly off disk. It does NOT call
`config.reload_config()`, so it captures whatever has been persisted to the
file — but in-memory overrides held only in the live bot process (anything
that was changed since the last save) are not visible. For a faithful
"what the running bot would do" snapshot, restart this script after a
`/reload` so the saved file is current.
"""
import asyncio
import json

async def main():
    with open("/app/config.json") as f:
        cfg = json.load(f)
    from eval_service import nightly_eval_worker
    await nightly_eval_worker(cfg)

asyncio.run(main())
