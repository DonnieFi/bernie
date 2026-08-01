"""database.conn — connection singleton, locks, path resolution (8lx.1)."""
from __future__ import annotations

import asyncio
import logging
import os as _os
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import sqlite_async

log = logging.getLogger("database.conn")

HFX = ZoneInfo("America/Halifax")
_ROOT = "/opt/family-bot" if _os.path.exists("/opt/family-bot/config.json") else "/app"
# Default only — tests MUST patch database.DB_PATH (package), not database.conn.DB_PATH.
# Runtime path resolution always goes through _resolve_db_path() → package.DB_PATH.
DB_PATH = f"{_ROOT}/data/family_bot.db" if _ROOT == "/opt/family-bot" else "/data/family_bot.db"

# ── family-bot-8lx.9: connection state owner ─────────────────────────────────
_STATE_ATTRS = (
    "_conn",
    "_async_conn",
    "_conn_path",
    "_lock",
    "_init_lock",
    "_active_loop",
)


class DatabaseManager:
    """Single owner of SQLite write-connection singleton state.

    Package module ``database`` still mirrors these attrs so existing tests that
    do ``database.DB_PATH = tmp`` / ``database._conn = None`` keep working.
    """

    __slots__ = _STATE_ATTRS

    def __init__(self) -> None:
        self._conn = None
        self._async_conn = None
        self._conn_path = None
        self._lock = None
        self._init_lock = None
        self._active_loop = None


_manager = DatabaseManager()


def get_manager() -> DatabaseManager:
    return _manager


def _hydrate_from_package(m: DatabaseManager) -> None:
    """Pull test patches from package __dict__ onto the manager."""
    import sys as _sys
    p = _sys.modules.get("database")
    if p is None:
        return
    d = getattr(p, "__dict__", None)
    if not d:
        return
    for attr in _STATE_ATTRS:
        if attr in d:
            setattr(m, attr, d[attr])


def _publish_to_package(m: DatabaseManager) -> None:
    """Mirror manager state onto package for tests reading database._conn."""
    import sys as _sys
    p = _sys.modules.get("database")
    if p is None:
        return
    d = p.__dict__
    for attr in _STATE_ATTRS:
        d[attr] = getattr(m, attr)


def _state() -> DatabaseManager:
    m = _manager
    _hydrate_from_package(m)
    return m


def _resolve_db_path() -> str:
    """Read DB_PATH from the database package module so tests that do
    ``database.DB_PATH = tmppath`` are respected at call time, even though
    this module is database._impl."""
    import sys as _sys
    pkg = _sys.modules.get('database')
    return getattr(pkg, 'DB_PATH', DB_PATH)

def _pkg():
    """Return the database package module (DB_PATH + legacy attrs)."""
    import sys as _sys
    return _sys.modules['database']


def _check_loop():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    m = _state()
    if m._active_loop is not loop:
        # Close orphaned sync handle before nulling (path-mismatch reopen does the same).
        # Sync close is best-effort: check_same_thread=False; unittest loop churn.
        _c = m._conn
        if _c is not None:
            try:
                _c.close()
            except Exception:
                pass
        m._conn = None
        m._async_conn = None
        m._conn_path = None
        m._lock = None
        m._init_lock = None
        m._active_loop = loop
        _publish_to_package(m)

def _get_lock() -> asyncio.Lock:
    _check_loop()
    m = _state()
    if m._lock is None:
        m._lock = asyncio.Lock()
        _publish_to_package(m)
    return m._lock

def _get_init_lock() -> asyncio.Lock:
    _check_loop()
    m = _state()
    if m._init_lock is None:
        m._init_lock = asyncio.Lock()
        _publish_to_package(m)
    return m._init_lock

async def _get_connection() -> sqlite_async.AsyncConnection:
    """Return the cached write connection, or open a new one against DB_PATH."""
    _check_loop()
    m = _state()
    _db = _resolve_db_path()
    _c = m._conn
    _ac = m._async_conn
    _cp = m._conn_path
    if _c is not None and _cp != _db:
        try:
            await asyncio.to_thread(_c.close)
        except Exception:
            pass
        m._conn = None
        m._async_conn = None
        m._conn_path = None
        sqlite_async.reset_journal_mode_cache()
        _c = _ac = _cp = None
        _publish_to_package(m)
    if _ac is not None:
        return _ac
    async with _get_init_lock():
        m = _state()
        _ac = m._async_conn
        _cp = m._conn_path
        _c = m._conn
        if _ac is None or _cp != _db:
            if _c is not None:
                try:
                    await asyncio.to_thread(_c.close)
                except Exception:
                    pass
            m._conn = await asyncio.to_thread(
                sqlite_async.open_write_connection, _db, timeout=5.0,
            )
            m._conn_path = _db
            m._async_conn = sqlite_async.AsyncConnection(
                m._conn, owns_connection=False, locked=True,
            )
            _publish_to_package(m)
    return m._async_conn

async def _log_lock_error(detail: str) -> None:
    """Fire-and-forget write to activity_log when SQLite is locked > busy_timeout."""
    import json as _json
    try:
        async with sqlite_async.connect(_resolve_db_path(), timeout=5.0) as _c:
            await _c.execute("PRAGMA busy_timeout=1000;")
            await _c.execute(
                "INSERT INTO activity_log (event_type, description, metadata) VALUES (?, ?, ?)",
                ("db_lock_error", "SQLite busy_timeout exceeded", _json.dumps({"detail": detail[:200]}))
            )
            await _c.commit()
    except Exception as _e:
        log.warning("db_lock_error: couldn't write to activity_log: %s", _e)

@asynccontextmanager
async def _db_conn():
    """Shared write connection context (40B-2A).
    Coroutines are serialised via asyncio.Lock at the boundary;
    actual sqlite3 writes are protected by threading.Lock inside sqlite_async.

    Successful exit always commits so a forgotten commit cannot leave a dirty
    transaction on the shared singleton for the next waiter. Explicit
    ``await conn.commit()`` inside the block remains fine (no-op if already
    committed). Exceptions roll back.
    """
    async with _get_lock():
        conn = await _get_connection()
        try:
            yield conn
        except Exception as exc:
            try:
                await conn.rollback()
            except Exception:
                pass
            if "database is locked" in str(exc).lower():
                log.error("db_lock_error: SQLite contention — %s", exc)
                import asyncio as _asyncio
                _asyncio.create_task(_log_lock_error(str(exc)))
            raise
        else:
            try:
                await conn.commit()
            except Exception as e:
                log.warning("db_conn: auto-commit on exit failed: %s", e)
                try:
                    await conn.rollback()
                except Exception:
                    pass
                raise

db_conn = _db_conn


@asynccontextmanager
async def _db_read():
    """Short-lived pure-SELECT connection (c79.1).

    Bypasses the write-path asyncio.Lock so concurrent readers do not queue
    behind writers. WAL allows concurrent reads. Do not use for writes.
    """
    async with sqlite_async.connect(_resolve_db_path(), timeout=10.0) as conn:
        yield conn


db_read = _db_read

async def close_db():
    """Close the shared database connection and clear the singleton."""
    async with _get_init_lock():
        m = _state()
        _c = m._conn
        if _c is not None:
            try:
                if sqlite_async.journal_mode_for_path(_resolve_db_path()) == "WAL":
                    await sqlite_async.wal_checkpoint(_c, "TRUNCATE")
            except Exception:
                pass
            try:
                await asyncio.to_thread(_c.close)
            except Exception:
                pass
            m._conn = None
            m._async_conn = None
            m._conn_path = None
        m._lock = None
        m._init_lock = None
        _publish_to_package(m)
    sqlite_async.reset_journal_mode_cache()

async def wal_checkpoint_passive() -> None:
    """Periodic WAL maintenance (40B-2A). No-op when journal_mode is DELETE.

    Takes the write-conn asyncio.Lock so checkpoint never races mid-fetch on the
    singleton connection (family-bot-x6c).
    """
    if sqlite_async.journal_mode_for_path(_resolve_db_path()) != "WAL":
        return
    async with _get_lock():
        # family-bot-8lx.9 review: always read through manager (_state), not _pkg()
        m = _state()
        if m._conn is None:
            return
        await sqlite_async.wal_checkpoint(m._conn, "PASSIVE")

