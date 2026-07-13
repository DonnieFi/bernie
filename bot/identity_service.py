"""
identity_service.py — Persistent identity graph backed by SQLite (Phase 30).

Design contract:
  - PersonRegistry (constants.py) is the fast in-memory read-only cache for call sites
  - IdentityService adds durability, confidence metadata, and relationship edges
  - Call sites needing read-only resolution: use person_registry.resolve()
  - Call sites needing persistence or evidence chains: use identity_service
  - If identity_service is unavailable, callers should fall back to PersonRegistry
    and log to #anvil. Every method here catches Exception and returns a safe
    default rather than raising, except upsert_* which propagate (callers in the
    migration script need to know if a write fails).

Confidence semantics:
  - 0.95 = config-seeded and human-verified (not 1.0 — avoids false immutability)
  - 0.70 = inferred from presence patterns, not verified
  - verified=True means a human explicitly confirmed this mapping
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from db_binding import get_database
import db_writes

log = logging.getLogger(__name__)


class IdentityService:
    """Async SQLite-backed identity graph service.

    Complements PersonRegistry — does not replace it. PersonRegistry is the
    fast in-memory cache; IdentityService adds persistence and evidence tracking.
    """

    async def is_available(self) -> bool:
        """Health check: returns True if DB is accessible, False on any error."""
        try:
            return await get_database().identity_health_check()
        except Exception as e:
            log.error(f"identity_service health check failed: {e}")
            return False

    async def resolve_entity(self, key: str) -> dict | None:
        """Low-level alias lookup.

        Returns: {"canonical_id": str, "confidence": float, "source": str, "verified": bool}
        or None if key is not found.

        Tries exact (case-insensitive) lookup first, then FTS5 fuzzy fallback.
        """
        if not key:
            return None
        try:
            return await get_database().identity_resolve_entity(key)
        except Exception as e:
            log.error(f"identity_service.resolve_entity({key!r}) failed: {e}")
            return None

    async def get_identity(self, canonical_id: str) -> dict | None:
        """Full identity node record.

        Returns: {"node_id": str, "canonical_id": str, "type": str,
                  "metadata": dict, "created_at": str, "updated_at": str}
        or None if not found.
        """
        if not canonical_id:
            return None
        try:
            return await get_database().identity_get_node(canonical_id)
        except Exception as e:
            log.error(f"identity_service.get_identity({canonical_id!r}) failed: {e}")
            return None

    async def get_identity_info(self, query: str) -> dict:
        """High-level traceable query — used as the Claude tool backend.

        Resolves any input (name, alias, MAC, Discord ID) to a full evidence chain.

        Returns:
          {
            "canonical_id": str | None,
            "confidence": float,
            "evidence": [{"alias": str, "source": str, "verified": bool, "added_at": str}],
            "error": str | None
          }
        """
        if not query:
            return {"canonical_id": None, "confidence": 0.0, "evidence": [], "error": "Empty query"}
        try:
            resolved = await self.resolve_entity(query)
            if not resolved:
                return {
                    "canonical_id": None,
                    "confidence": 0.0,
                    "evidence": [],
                    "error": f"No identity found for {query!r}",
                }

            canonical_id = resolved["canonical_id"]
            evidence = await get_database().identity_list_aliases(canonical_id)

            return {
                "canonical_id": canonical_id,
                "confidence": resolved["confidence"],
                "evidence": evidence,
                "error": None,
            }
        except Exception as e:
            log.error(f"identity_service.get_identity_info({query!r}) failed: {e}")
            return {"canonical_id": None, "confidence": 0.0, "evidence": [], "error": str(e)}

    async def log_unresolved_entity(
        self, entity_key: str, entity_type: str, context: dict | None = None
    ) -> None:
        """Log an unknown entity for later human review.

        Upserts to unresolved_entities: inserts on first encounter, updates
        last_seen and increments count on repeat encounters.
        """
        if not entity_key or not entity_type:
            return
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            context_json = json.dumps(context or {})
            await db_writes.routed("identity_log_unresolved", 
                entity_key, entity_type, context_json, now_iso
            )
        except Exception as e:
            log.error(f"identity_service.log_unresolved_entity({entity_key!r}) failed: {e}")

    async def upsert_node(
        self,
        canonical_id: str,
        node_type: str = "person",
        metadata: dict | None = None,
    ) -> str:
        """Create or update an identity node. Returns node_id (UUID).

        Used by the migration script. Overwrites metadata on existing nodes
        (callers should pass the full desired metadata).
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(metadata or {})
        try:
            return await get_database().identity_upsert_node(
                canonical_id, node_type, meta_json, now_iso
            )
        except Exception as e:
            log.error(f"identity_service.upsert_node({canonical_id!r}) failed: {e}")
            raise

    async def upsert_alias(
        self,
        alias: str,
        node_id: str,
        confidence: float = 0.95,
        source: str = "config",
        verified: bool = True,
    ) -> None:
        """Create or ignore an alias mapping. INSERT OR IGNORE = safe to re-run.

        Also indexes the alias into the FTS5 identity_search table for fuzzy lookups.
        """
        if not alias or not node_id:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        alias_lc = alias.lower()
        try:
            await get_database().identity_upsert_alias(
                alias_lc, node_id, confidence, source, int(verified), now_iso
            )
        except Exception as e:
            log.error(f"identity_service.upsert_alias({alias!r}) failed: {e}")
            raise

    async def upsert_edge(
        self,
        source_id: str,
        target_id: str,
        rel_type: str,
        confidence: float = 0.95,
        evidence: str = "",
        verified: bool = True,
    ) -> None:
        """Create a semantic relationship edge if one of the same shape doesn't exist.

        Idempotency: skips the insert if an edge with the same (source, target, rel_type)
        is already present, so re-running the migration is safe.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            await get_database().identity_upsert_edge(
                str(uuid.uuid4()),
                source_id,
                target_id,
                rel_type,
                confidence,
                evidence,
                int(verified),
                now_iso,
            )
        except Exception as e:
            log.error(
                f"identity_service.upsert_edge({source_id!r} -{rel_type}-> {target_id!r}) failed: {e}"
            )
            raise


identity_service = IdentityService()
