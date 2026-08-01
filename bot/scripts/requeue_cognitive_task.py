#!/usr/bin/env python3
"""Re-queue a failed/dead_letter cognitive task (run on bernie-host / cognition container)."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Container cwd is /app; host runs may need explicit path.
_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP not in sys.path:
    sys.path.insert(0, _APP)


async def _main(task_id: int) -> int:
    from db_binding import get_database

    database = get_database()
    await database.init_db()
    ok = await database.requeue_cognitive_task(task_id)
    if not ok:
        print(f"task {task_id}: not found or not in failed/dead_letter", file=sys.stderr)
        return 1
    row = await database.get_cognitive_task(task_id)
    print(f"requeued task {task_id}: type={row.get('type')} payload={row.get('payload')}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", type=int)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.task_id)))
