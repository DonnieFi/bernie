"""Async façade over sync sqlite3 (40B-2A).

Replaces aiosqlite: each operation runs in asyncio.to_thread against a sync
connection using threading.Lock for write serialisation (per SPEC Appendix A).
Short-lived read connections bypass the write lock; writes use one shared
connection protected by threading.Lock.

8lx.5 decision: keep this facade. It is the shared AsyncConnection/Cursor +
journal_mode/WAL helpers used by database.conn and every domain. Deleting it
would re-inline ~200 lines of lock/to_thread ceremony into conn.py with no
behavior win — not YAGNI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

Row = sqlite3.Row

_WRITE_LOCK = threading.Lock()
_JOURNAL_MODE: str | None = None


def journal_mode_for_path(db_path: str) -> str:
    """WAL on local disk; DELETE on NFS/CIFS (40-SPEC Appendix A)."""
    global _JOURNAL_MODE
    if _JOURNAL_MODE is not None:
        return _JOURNAL_MODE
    mode = "WAL"
    try:
        import subprocess

        out = subprocess.run(
            ["df", "-T", os.path.dirname(os.path.abspath(db_path))],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode == 0:
            for line in out.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1].lower() in ("nfs", "nfs4", "cifs", "smbfs"):
                    mode = "DELETE"
                    log.info("sqlite_async: %s on %s — using journal_mode=DELETE", db_path, parts[1])
                    break
    except Exception as exc:
        log.debug("sqlite_async: filesystem probe failed (%s); default journal_mode=WAL", exc)
    _JOURNAL_MODE = mode
    return mode


def _open_sync_connection(path: str, *, timeout: float, row_factory: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    if row_factory:
        conn.row_factory = sqlite3.Row
    busy_ms = min(max(int(timeout * 1000), 1000), 5000)
    conn.execute(f"PRAGMA busy_timeout={busy_ms};")
    conn.execute(f"PRAGMA journal_mode={journal_mode_for_path(path)};")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA wal_autocheckpoint=1000;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    return conn


class AsyncCursor:
    def __init__(self, cursor: sqlite3.Cursor, conn: "AsyncConnection"):
        self._cursor = cursor
        self._conn = conn

    async def fetchone(self):
        # Must hold the same lock as execute — GIL is released during sqlite fetch.
        return await asyncio.to_thread(self._conn._run_locked, self._cursor.fetchone)

    async def fetchall(self):
        return await asyncio.to_thread(self._conn._run_locked, self._cursor.fetchall)

    def __aiter__(self) -> AsyncIterator:
        return self

    async def __anext__(self):
        row = await asyncio.to_thread(self._conn._run_locked, self._cursor.fetchone)
        if row is None:
            raise StopAsyncIteration
        return row

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int:
        return self._cursor.lastrowid


class _ExecuteCM:
    """Supports both ``await conn.execute()`` and ``async with conn.execute()``."""

    def __init__(self, conn: "AsyncConnection", sql: str, params: Any):
        self._conn = conn
        self._sql = sql
        self._params = params
        self._cursor: sqlite3.Cursor | None = None

    def _run(self) -> sqlite3.Cursor:
        return self._conn._run_locked(lambda: self._conn._conn.execute(self._sql, self._params))

    async def _cursor_wrapper(self) -> AsyncCursor:
        self._cursor = await asyncio.to_thread(self._run)
        return AsyncCursor(self._cursor, self._conn)

    def __await__(self):
        return self._cursor_wrapper().__await__()

    async def __aenter__(self) -> AsyncCursor:
        return await self._cursor_wrapper()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._cursor is not None:
            await asyncio.to_thread(self._conn._run_locked, self._cursor.close)
        return False


class _ExecutemanyCM:
    def __init__(self, conn: "AsyncConnection", sql: str, params):
        self._conn = conn
        self._sql = sql
        self._params = params
        self._cursor: sqlite3.Cursor | None = None

    def _run(self) -> sqlite3.Cursor:
        return self._conn._run_locked(
            lambda: self._conn._conn.executemany(self._sql, self._params)
        )

    async def _cursor_wrapper(self) -> AsyncCursor:
        self._cursor = await asyncio.to_thread(self._run)
        return AsyncCursor(self._cursor, self._conn)

    def __await__(self):
        return self._cursor_wrapper().__await__()

    async def __aenter__(self) -> AsyncCursor:
        return await self._cursor_wrapper()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._cursor is not None:
            await asyncio.to_thread(self._conn._run_locked, self._cursor.close)
        return False


class AsyncConnection:
    """Mimics the previous aiosqlite surface (for 40B-2A compatibility)."""

    def __init__(self, conn: sqlite3.Connection, *, owns_connection: bool, locked: bool = False):
        self._conn = conn
        self._owns = owns_connection
        self._locked = locked
        self.row_factory = sqlite3.Row

    def _run_locked(self, fn):
        if self._locked:
            with _WRITE_LOCK:
                return fn()
        return fn()

    def execute(self, sql: str, params: Any = ()):
        return _ExecuteCM(self, sql, params)

    def executemany(self, sql: str, params):
        return _ExecutemanyCM(self, sql, params)

    async def executescript(self, script: str):
        def _op():
            return self._run_locked(lambda: self._conn.executescript(script))

        await asyncio.to_thread(_op)

    async def commit(self):
        await asyncio.to_thread(self._run_locked, self._conn.commit)

    async def rollback(self):
        await asyncio.to_thread(self._run_locked, self._conn.rollback)

    async def close(self):
        if not self._owns:
            return

        def _close():
            with _WRITE_LOCK:
                self._conn.close()

        await asyncio.to_thread(_close)


@asynccontextmanager
async def connect(path: str, timeout: float = 5.0):
    """Short-lived connection (read paths, VACUUM, lock-error logging)."""
    conn = await asyncio.to_thread(_open_sync_connection, path, timeout=timeout)
    wrapper = AsyncConnection(conn, owns_connection=True, locked=False)
    try:
        yield wrapper
    finally:
        await wrapper.close()


def open_write_connection(path: str, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Sync open for the process write singleton (called via to_thread once)."""
    return _open_sync_connection(path, timeout=timeout)


async def wal_checkpoint(conn: sqlite3.Connection, mode: str = "PASSIVE") -> None:
    def _cp():
        with _WRITE_LOCK:
            conn.execute(f"PRAGMA wal_checkpoint({mode});")

    await asyncio.to_thread(_cp)


def reset_journal_mode_cache() -> None:
    """Tests that swap DB_PATH on different filesystems."""
    global _JOURNAL_MODE
    _JOURNAL_MODE = None
