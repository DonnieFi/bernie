"""Phase 30 — IdentityService unit tests.

Uses unittest.IsolatedAsyncioTestCase to match project convention. Each test
gets a fresh temp SQLite DB via asyncSetUp (the `identity_db` fixture pattern
called out in plan 30-01 is satisfied here by self.identity_db).
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import sqlite_async
import sqlite3

try:
    import database
    import identity_service as id_svc_mod
    from identity_service import IdentityService
except ModuleNotFoundError:
    sqlite_async = None
    database = None
    id_svc_mod = None
    IdentityService = None


@unittest.skipUnless(sqlite_async and database and IdentityService,
                     "sqlite_async or project modules not available")
class TestIdentityService(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.identity_db = os.path.join(self._tmpdir.name, "test_identity.db")
        self._old_db_path = database.DB_PATH
        # log_unresolved_entity goes through db_writes.routed; under ROLE=api
        # (bernie-api container) writes RPC to cognition and never hit this temp DB.
        self._old_role = os.environ.get("ROLE")
        os.environ["ROLE"] = "monolith"
        database.DB_PATH = self.identity_db
        await database.init_db()
        self.svc = IdentityService()

    async def asyncTearDown(self):
        if hasattr(database, 'close_db'):
            await database.close_db()
        database.DB_PATH = self._old_db_path
        if self._old_role is None:
            os.environ.pop("ROLE", None)
        else:
            os.environ["ROLE"] = self._old_role
        self._tmpdir.cleanup()

    async def _insert_node(self, canonical_id, node_type="person", metadata=None):
        return await self.svc.upsert_node(canonical_id, node_type, metadata or {})

    async def _insert_alias(self, alias, node_id, source="config", confidence=0.95, verified=True):
        await self.svc.upsert_alias(alias, node_id, confidence, source, verified)

    async def test_schema(self):
        """All 4 identity tables + FTS5 search table exist after init_db()."""
        async with sqlite_async.connect(self.identity_db) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','virtual')"
            ) as cur:
                tables = {row[0] for row in await cur.fetchall()}
        self.assertIn("identity_nodes", tables)
        self.assertIn("identity_aliases", tables)
        self.assertIn("identity_edges", tables)
        self.assertIn("unresolved_entities", tables)
        self.assertIn("identity_search", tables)

    async def test_resolve_entity(self):
        """resolve_entity returns full evidence dict for a known alias."""
        node_id = await self._insert_node("dad")
        await self._insert_alias("Dad", node_id, source="config")

        result = await self.svc.resolve_entity("Dad")
        self.assertEqual(result["canonical_id"], "dad")
        self.assertEqual(result["confidence"], 0.95)
        self.assertEqual(result["source"], "config")
        self.assertTrue(result["verified"])

        # Case-insensitive
        result_upper = await self.svc.resolve_entity("DAD")
        self.assertEqual(result_upper["canonical_id"], "dad")

    async def test_resolve_entity_miss(self):
        """resolve_entity returns None for unknown keys."""
        self.assertIsNone(await self.svc.resolve_entity("nonexistent_key"))
        self.assertIsNone(await self.svc.resolve_entity(""))
        self.assertIsNone(await self.svc.resolve_entity(None))

    async def test_get_identity(self):
        """get_identity returns full node record with deserialized metadata."""
        node_id = await self._insert_node("mom", "person", {"name": "Mom", "role": "parent"})

        identity = await self.svc.get_identity("mom")
        self.assertIsNotNone(identity)
        self.assertEqual(identity["node_id"], node_id)
        self.assertEqual(identity["canonical_id"], "mom")
        self.assertEqual(identity["type"], "person")
        self.assertIsInstance(identity["metadata"], dict)
        self.assertEqual(identity["metadata"]["name"], "Mom")
        self.assertIn("created_at", identity)
        self.assertIn("updated_at", identity)

        # Miss
        self.assertIsNone(await self.svc.get_identity("nonexistent"))

    async def test_health_check(self):
        """is_available() returns True against a healthy DB, False when the
        underlying connection fails."""
        self.assertTrue(await self.svc.is_available())

        # Simulate a broken DB by making the connection helper raise
        original = database.identity_health_check
        async def _boom(*args, **kwargs):
            raise RuntimeError("simulated DB failure")
        database.identity_health_check = _boom
        try:
            self.assertFalse(await self.svc.is_available())
        finally:
            database.identity_health_check = original

    async def test_unresolved_logging(self):
        """log_unresolved_entity upserts (insert then increment) on repeat encounters."""
        await self.svc.log_unresolved_entity("AA:BB:CC:DD:EE:FF", "mac", {"seen_in": "wifi_scan"})

        async with sqlite_async.connect(self.identity_db) as db:
            db.row_factory = sqlite3.Row
            async with db.execute(
                "SELECT * FROM unresolved_entities WHERE entity_key=?",
                ("AA:BB:CC:DD:EE:FF",),
            ) as cur:
                row = await cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["type"], "mac")
        self.assertEqual(row["count"], 1)
        first_seen = row["first_seen"]

        await self.svc.log_unresolved_entity("AA:BB:CC:DD:EE:FF", "mac", {"seen_in": "wifi_scan2"})

        async with sqlite_async.connect(self.identity_db) as db:
            db.row_factory = sqlite3.Row
            async with db.execute(
                "SELECT * FROM unresolved_entities WHERE entity_key=?",
                ("AA:BB:CC:DD:EE:FF",),
            ) as cur:
                row2 = await cur.fetchone()
        self.assertEqual(row2["count"], 2)
        self.assertEqual(row2["first_seen"], first_seen)  # unchanged
        # last_seen updated (>= first_seen)
        self.assertGreaterEqual(row2["last_seen"], first_seen)

    async def test_migration_idempotent(self):
        """upsert_node twice with same canonical_id yields one row."""
        node_id_a = await self._insert_node("child1", "person", {"name": "Child1"})
        node_id_b = await self._insert_node("child1", "person", {"name": "Child1", "role": "child"})

        # Same node_id returned both times
        self.assertEqual(node_id_a, node_id_b)

        async with sqlite_async.connect(self.identity_db) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM identity_nodes WHERE canonical_id=?",
                ("child1",),
            ) as cur:
                (count,) = await cur.fetchone()
        self.assertEqual(count, 1)

        # Metadata updated on second call
        identity = await self.svc.get_identity("child1")
        self.assertEqual(identity["metadata"]["role"], "child")

    async def test_alias_dedup(self):
        """upsert_alias twice with same alias yields one row (INSERT OR IGNORE)."""
        node_id = await self._insert_node("child2", "person")
        await self._insert_alias("Child2", node_id, source="config")
        await self._insert_alias("Child2", node_id, source="config")  # duplicate
        await self._insert_alias("child2", node_id, source="config")  # same lowercase key

        async with sqlite_async.connect(self.identity_db) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM identity_aliases WHERE node_id=?",
                (node_id,),
            ) as cur:
                (count,) = await cur.fetchone()
        self.assertEqual(count, 1)

    async def test_edge_upsert(self):
        """upsert_edge creates relationship and is idempotent."""
        person_id = await self._insert_node("dad", "person")
        device_id = await self._insert_node("dev_aabbccddeeff", "device", {"mac": "AA:BB:CC:DD:EE:FF"})

        await self.svc.upsert_edge(device_id, person_id, "owned_by", evidence="config.device_macs")
        await self.svc.upsert_edge(device_id, person_id, "owned_by", evidence="config.device_macs")

        async with sqlite_async.connect(self.identity_db) as db:
            async with db.execute(
                """SELECT COUNT(*) FROM identity_edges
                   WHERE source_id=? AND target_id=? AND rel_type='owned_by'""",
                (device_id, person_id),
            ) as cur:
                (count,) = await cur.fetchone()
        self.assertEqual(count, 1)

    async def test_get_identity_info_evidence_chain(self):
        """get_identity_info returns canonical_id with full evidence chain."""
        node_id = await self._insert_node("dad", "person", {"name": "Dad"})
        await self._insert_alias("Dad", node_id, source="config")
        await self._insert_alias("person.red", node_id, source="ha")

        info = await self.svc.get_identity_info("Dad")
        self.assertEqual(info["canonical_id"], "dad")
        self.assertIsNone(info["error"])
        self.assertEqual(info["confidence"], 0.95)
        aliases = {e["alias"] for e in info["evidence"]}
        self.assertIn("dad", aliases)
        self.assertIn("person.red", aliases)
        for e in info["evidence"]:
            self.assertIn("source", e)
            self.assertIn("verified", e)
            self.assertIn("added_at", e)

        # Miss case returns error not None
        miss = await self.svc.get_identity_info("nobody")
        self.assertIsNone(miss["canonical_id"])
        self.assertIsNotNone(miss["error"])

    async def test_seed_from_config(self):
        """seed_from_config seeds person nodes, aliases, and owned_by device edges."""
        import migrate_identity as mig
        old_path = mig.identity_service.__class__.__module__

        cfg = {
            "family_members": {
                "Dad": {
                    "canonical_id": "dad",
                    "first_name": "Dad",
                    "role": "admin",
                    "email": "d@example.com",
                    "ha_entity": "person.red",
                    "discord_id": 123456,
                    "aliases": ["Dad", "Don"],
                    "device_macs": ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"],
                }
            }
        }

        result = await mig.seed_from_config(cfg)
        self.assertEqual(result["seeded"], 1)

        # Person node exists
        node = await self.svc.get_identity("dad")
        self.assertIsNotNone(node)
        self.assertEqual(node["type"], "person")
        self.assertEqual(node["metadata"]["role"], "admin")

        # Aliases: canonical_id, first_name, ha_entity, discord_id, custom aliases
        for alias in ["dad", "dad", "don", "person.red", "123456"]:
            resolved = await self.svc.resolve_entity(alias)
            self.assertIsNotNone(resolved, f"alias {alias!r} not found")
            self.assertEqual(resolved["canonical_id"], "dad")

        # Device nodes + owned_by edges
        async with sqlite_async.connect(self.identity_db) as db:
            async with db.execute("SELECT COUNT(*) FROM identity_edges WHERE rel_type='owned_by'") as cur:
                (edge_count,) = await cur.fetchone()
        self.assertEqual(edge_count, 2)

        # Idempotent — second run does not duplicate
        await mig.seed_from_config(cfg)
        async with sqlite_async.connect(self.identity_db) as db:
            async with db.execute("SELECT COUNT(*) FROM identity_nodes WHERE type='person'") as cur:
                (person_count,) = await cur.fetchone()
        self.assertEqual(person_count, 1)


if __name__ == "__main__":
    unittest.main()
