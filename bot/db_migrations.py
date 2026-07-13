"""40B-2B: versioned brownfield schema migrations.

Replaces the ad-hoc ensure_* chain in init_db() brownfield startup. Each migration
is idempotent; applied versions are recorded in schema_migrations.

**Hard rule (family-bot-2j9):** every migration function MUST be idempotent forever.
``ensure_*`` helpers open their own ``_db_conn()`` and commit; apply then record are
sequential commits, not one SQLite transaction. A crash between apply and record
re-runs the migration on next boot — safe only if re-apply is a no-op.
Future data transforms that cannot be re-run must ship as a single function that
applies + records under one ``_db_conn()`` commit (do not add non-idempotent steps
to the ensure_* list without that pattern).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

# (version, name, database.ensure_* function name)
# All ensure_* entries must be idempotent — see module docstring.
MIGRATION_SPECS: list[tuple[int, str, str]] = [
    (1, "email_schema", "ensure_email_schema"),
    (2, "pending_hitl_schema", "ensure_pending_hitl_schema"),
    (3, "pending_notifications_schema", "ensure_pending_notifications_schema"),
    (4, "turn_instrumentation_schema", "ensure_turn_instrumentation_schema"),
    (5, "db_metadata_schema", "ensure_db_metadata_schema"),
    (6, "legacy_schema_cleanup", "ensure_legacy_schema_cleanup"),
    (7, "network_watchman_schema", "ensure_network_watchman_schema"),
    # 8lx.4 + 5hy.11: FTS5 on conversation_history (session_search)
    (8, "conversation_history_fts", "ensure_conversation_history_fts"),
    # c79.2: activity_log (event_type, logged_at) composite index
    (9, "activity_log_event_time_index", "ensure_activity_log_event_time_index"),
]


def _validate_migration_specs() -> None:
    import database as db

    for version, name, fn_name in MIGRATION_SPECS:
        if not hasattr(db, fn_name):
            raise RuntimeError(
                f"schema migration v{version} ({name}): missing database.{fn_name}"
            )


_validate_migration_specs()


async def run_schema_migrations() -> None:
    """Apply pending migrations in version order; record each after apply.

    Apply + record are not a single transaction (ensure_* commit internally).
    Contract: migrations are idempotent so crash-between is safe (see module doc).
    """
    import database as db

    applied = await db.get_applied_schema_migration_versions()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for version, name, fn_name in MIGRATION_SPECS:
        if version in applied:
            continue
        fn: Callable[[], Awaitable[None]] = getattr(db, fn_name)
        # Sequential apply then record; INSERT OR IGNORE on record is extra safety.
        await fn()
        await db.record_schema_migration(version, name, now)
        log.info("schema_migration applied: v%d %s", version, name)
