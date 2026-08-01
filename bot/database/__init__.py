"""Bernie database package (family-bot-8lx.1 / 8lx.7 / 8lx.8 / 8lx.9).

Domain split lives under ``database.<domain>``. This package re-exports the
public surface from ``_impl`` so:

- ``import database`` + ``db_binding.bind_database`` (main) stay one-shot
- ``cognition_write.WRITE_OPS`` dynamic ``getattr(database, op)`` keeps working
- tests may still ``import database`` and patch ``database.DB_PATH`` / ``_conn``

family-bot-8lx.8: production call-sites prefer domain imports where static;
the package facade is **retained on purpose** (not deleted) for allowlist
dispatch and the hundreds of test imports.

family-bot-8lx.9: connection singleton state is owned by
``database.conn.DatabaseManager``; package attrs mirror for test patches.

family-bot-8lx.7: public re-exports are generated from ``database._impl``.
"""

# ── Connection state mirrors (tests patch these; manager is source of truth) ─
_conn = None
_async_conn = None
_conn_path = None
_lock = None
_init_lock = None
_active_loop = None

# Private helpers re-exported for tests / internal callers (star-import skips _)
_PRIVATE_REEXPORT = frozenset({
    "_db_conn",
    "_db_read",
    "_pkg",
    "_resolve_db_path",
    "_get_connection",
    "_get_lock",
    "_get_init_lock",
    "_check_loop",
    "_log_lock_error",
    "_person_id_for_discord",
    "_row_to_task",
    "_row_to_automation",
    "_row_to_draft",
    "_set_last_vacuum_at",
    "_token_cost",
    "_col_exists",
    "_table_exists",
    "_db_already_initialized",
    "_legacy_tasks_fully_migrated",
    "_system_task_row_from_cognitive",
    "_reachable",
    "_load_price_index",
    "_PRICE_EXACT",
    "_PRICE_FRAGS",
})

from database import _impl as _impl  # noqa: E402

__all__: list[str] = []
for _name in dir(_impl):
    if _name.startswith("__"):
        continue
    if _name.startswith("_") and _name not in _PRIVATE_REEXPORT:
        continue
    # Do not clobber package-level connection state if _impl ever defines these
    if _name in {
        "_conn", "_async_conn", "_conn_path", "_lock", "_init_lock", "_active_loop",
    }:
        continue
    globals()[_name] = getattr(_impl, _name)
    __all__.append(_name)

__all__ = sorted(set(__all__))
del _name
