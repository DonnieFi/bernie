"""Scan for direct SQLite writes on discord/api paths (40A-5b audit gate)."""
from __future__ import annotations

import os
import re
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent

WRITE_PREFIXES = (
    "add_", "create_", "update_", "delete_", "remove_", "set_", "save_", "store_",
    "mark_", "insert_", "upsert_", "record_", "log_", "prune_", "purge_", "expire_",
    "decay_", "resolve_",
    "claim_", "finalize_", "ensure_", "init_db", "identity_log", "link_", "complete_",
    "approve_", "snooze_", "block_", "reassign_", "convert_", "promote_", "start_",
    "finish_", "execute",
)

COGNITION_ONLY_SUFFIXES = (
    "worker.py",
    "nightly_digest.py",
    "cognitive_workers/consolidation.py",
    "cognitive_workers/reflection.py",
    "cognitive_workers/research.py",
    "cognitive_workers/study_detection.py",
    "cognitive_workers/study_guide.py",
)

MONOLITH_ONLY_LINES = {
    ("bot.py", "init_db"),
    ("bot.py", "ensure_email_schema"),
    ("bot.py", "ensure_pending_hitl_schema"),
    ("bot.py", "ensure_network_watchman_schema"),
}


def is_write(name: str) -> bool:
    if name in ("reload_model_prices",):
        return False
    if name.startswith(("get_", "list_", "is_", "count_")):
        return False
    return any(name.startswith(p) for p in WRITE_PREFIXES) or name in WRITE_PREFIXES


def cognition_only(rel: str) -> bool:
    return any(rel.endswith(s) or s in rel for s in COGNITION_ONLY_SUFFIXES)


def scan(bot_dir: Path | None = None) -> tuple[list[tuple[str, int, str, str]], list[tuple[str, int, str, str]]]:
    """Return (stragglers, cognition_only_hits) relative to the bot package root."""
    root = bot_dir or BOT_DIR
    stragglers: list[tuple[str, int, str, str]] = []
    cog_hits: list[tuple[str, int, str, str]] = []
    pat = re.compile(r"get_database\(\)\.(\w+)|await db\.(\w+)|await c\.(execute|executescript)")

    for dirpath, _, files in os.walk(root):
        if "tests" in Path(dirpath).parts:
            continue
        for fname in files:
            if not fname.endswith(".py") or fname == "database.py":
                continue
            if fname == "migrate_tasks_v32.py":
                continue
            path = Path(dirpath) / fname
            rel = str(path.relative_to(root))
            # database package + schema migrations are the write implementation layer
            if rel == "db_migrations.py" or rel.startswith("database/") or rel.startswith("database\\"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if "db_writes" in line:
                    continue
                for m in pat.finditer(line):
                    fn = m.group(1) or m.group(2) or m.group(3)
                    if not is_write(fn):
                        continue
                    if (rel, fn) in MONOLITH_ONLY_LINES or (
                        rel == "bot.py" and fn.startswith("ensure_")
                    ):
                        continue
                    entry = (rel, i, fn, line.strip()[:100])
                    if cognition_only(rel):
                        cog_hits.append(entry)
                    else:
                        stragglers.append(entry)
    return stragglers, cog_hits
