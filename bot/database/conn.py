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

def _resolve_db_path() -> str:
    """Read DB_PATH from the database package module so tests that do
    ``database.DB_PATH = tmppath`` are respected at call time, even though
    this module is database._impl."""
    import sys as _sys
    pkg = _sys.modules.get('database')
    return getattr(pkg, 'DB_PATH', DB_PATH)

def _pkg():
    import sys as _sys
    return _sys.modules['database']

def _check_loop():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _p = _pkg()
    if getattr(_p, '_active_loop', None) is not loop:
        # Close orphaned sync handle before nulling (path-mismatch reopen does the same).
        # Sync close is best-effort: check_same_thread=False; unittest loop churn.
        _c = getattr(_p, '_conn', None)
        if _c is not None:
            try:
                _c.close()
            except Exception:
                pass
        _p._conn = None
        _p._async_conn = None
        _p._conn_path = None
        _p._lock = None
        _p._init_lock = None
        _p._active_loop = loop

def _get_lock() -> asyncio.Lock:
    _check_loop()
    _p = _pkg()
    if getattr(_p, '_lock', None) is None:
        _p._lock = asyncio.Lock()
    return _p._lock

def _get_init_lock() -> asyncio.Lock:
    _check_loop()
    _p = _pkg()
    if getattr(_p, '_init_lock', None) is None:
        _p._init_lock = asyncio.Lock()
    return _p._init_lock

async def _get_connection() -> sqlite_async.AsyncConnection:
    """Return the cached write connection, or open a new one against DB_PATH."""
    _check_loop()
    _p = _pkg()
    _db = _resolve_db_path()
    _c = getattr(_p, '_conn', None)
    _ac = getattr(_p, '_async_conn', None)
    _cp = getattr(_p, '_conn_path', None)
    if _c is not None and _cp != _db:
        try:
            await asyncio.to_thread(_c.close)
        except Exception:
            pass
        _p._conn = None
        _p._async_conn = None
        _p._conn_path = None
        sqlite_async.reset_journal_mode_cache()
        _c = _ac = _cp = None
    if _ac is not None:
        return _ac
    async with _get_init_lock():
        _ac = getattr(_p, '_async_conn', None)
        _cp = getattr(_p, '_conn_path', None)
        _c = getattr(_p, '_conn', None)
        if _ac is None or _cp != _db:
            if _c is not None:
                try:
                    await asyncio.to_thread(_c.close)
                except Exception:
                    pass
            _p._conn = await asyncio.to_thread(
                sqlite_async.open_write_connection, _db, timeout=5.0,
            )
            _p._conn_path = _db
            _p._async_conn = sqlite_async.AsyncConnection(
                _p._conn, owns_connection=False, locked=True,
            )
    return _p._async_conn

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
    _p = _pkg()
    async with _get_init_lock():
        _c = getattr(_p, '_conn', None)
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
            _p._conn = None
            _p._async_conn = None
            _p._conn_path = None
    sqlite_async.reset_journal_mode_cache()
    _p._lock = None
    _p._init_lock = None

async def wal_checkpoint_passive() -> None:
    """Periodic WAL maintenance (40B-2A). No-op when journal_mode is DELETE.

    Takes the write-conn asyncio.Lock so checkpoint never races mid-fetch on the
    singleton connection (family-bot-x6c).
    """
    if sqlite_async.journal_mode_for_path(_resolve_db_path()) != "WAL":
        return
    async with _get_lock():
        if getattr(_pkg(), '_conn', None) is None:
            return
        await sqlite_async.wal_checkpoint(_pkg()._conn, "PASSIVE")

