"""family-bot-8lx.8 / 8lx.9: domain migration + DatabaseManager.

Prefer behavioral import/resolve checks over source-string greps.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class Test8lx8CallSites(unittest.TestCase):
    def test_get_pending_hitl_resolves_from_misc(self):
        """internal_discord domain import must resolve at runtime."""
        from database.misc import get_pending_hitl
        import inspect
        self.assertTrue(inspect.iscoroutinefunction(get_pending_hitl))

    def test_db_conn_resolves_from_conn(self):
        from database.conn import _db_conn
        import inspect
        # async context manager
        self.assertTrue(hasattr(_db_conn, "__aenter__") or callable(_db_conn))

    def test_internal_discord_module_imports(self):
        """Importing internal_discord succeeds and binds domain get_pending_hitl."""
        import importlib
        mod = importlib.import_module("internal_discord")
        self.assertIn("get_pending_hitl", mod.__dict__)
        self.assertNotIn("database", mod.__dict__)

    def test_package_facade_retained_for_write_ops(self):
        import database
        self.assertTrue(hasattr(database, "init_db"))
        self.assertTrue(hasattr(database, "add_message") or hasattr(database, "_db_conn"))


class Test8lx9Manager(unittest.IsolatedAsyncioTestCase):
    def test_manager_exists(self):
        from database.conn import DatabaseManager, get_manager
        m = get_manager()
        self.assertIsInstance(m, DatabaseManager)

    def test_package_conn_patch_hydrates(self):
        import database
        from database.conn import get_manager, _state, _publish_to_package
        m = get_manager()
        prev = m._conn
        try:
            database._conn = None
            m2 = _state()
            self.assertIsNone(m2._conn)
        finally:
            m._conn = prev
            _publish_to_package(m)

    def test_wal_checkpoint_uses_state_not_pkg_only(self):
        """wal_checkpoint_passive source must call _state() (review fix)."""
        import inspect
        from database import conn as conn_mod
        src = inspect.getsource(conn_mod.wal_checkpoint_passive)
        self.assertIn("_state()", src)
        self.assertNotIn("_pkg()._conn", src)

    async def test_close_db_clears_manager(self):
        """close_db clears manager locks/conn (behavioral reopen contract)."""
        import database
        from database.conn import get_manager, close_db, _publish_to_package
        m = get_manager()
        # Don't open a real connection — just set sentinel state and clear
        sentinel_lock = object()
        m._lock = sentinel_lock  # type: ignore
        m._init_lock = object()  # type: ignore
        _publish_to_package(m)
        # close_db needs init lock as real asyncio.Lock — set real one
        import asyncio
        m._init_lock = asyncio.Lock()
        m._lock = asyncio.Lock()
        m._conn = None
        _publish_to_package(m)
        await close_db()
        m2 = get_manager()
        self.assertIsNone(m2._conn)
        self.assertIsNone(m2._lock)
        self.assertIsNone(m2._init_lock)


if __name__ == "__main__":
    unittest.main()
