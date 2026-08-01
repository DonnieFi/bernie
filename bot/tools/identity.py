"""Identity graph tool handlers — all READ."""
from __future__ import annotations

import json
import logging

from tools import ROLE_ADMIN, ROLE_ALL, tool

log = logging.getLogger(__name__)


@tool(
    name="get_identity_info",
    description=(
        "Look up who or what an identifier belongs to. Returns a canonical "
        "identity with a full evidence chain."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Name, alias, Discord ID, MAC address, HA entity"},
        },
        "required": ["query"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_identity_info(args: dict, ctx) -> str:
    from identity_service import identity_service
    from constants import registry as person_registry

    query = args.get("query", "").strip()
    if not query:
        return "Please provide a query to look up."
    try:
        result = await identity_service.get_identity_info(query)
        if result.get("error"):
            fallback = person_registry.resolve(query)
            if fallback:
                return f"Identity graph miss — PersonRegistry fallback: {fallback}"
            return f"Unknown identifier: {query!r}"
        lines = [
            f"canonical_id: {result['canonical_id']}",
            f"confidence: {result['confidence']:.2f}",
            f"evidence ({len(result['evidence'])} aliases):",
        ]
        for ev in result["evidence"][:8]:
            verified = "✓" if ev["verified"] else "~"
            lines.append(f"  {verified} {ev['alias']} (source: {ev['source']})")
        return "\n".join(lines)
    except Exception as e:
        log.error("get_identity_info tool failed: %s", e)
        return f"Identity lookup failed: {e}"


@tool(
    name="resolve_entity",
    description=(
        "Low-level alias lookup. Returns canonical_id and confidence for any "
        "known identifier."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Name, MAC, Discord ID, etc."},
        },
        "required": ["key"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_resolve_entity(args: dict, ctx) -> str:
    from identity_service import identity_service
    from constants import registry as person_registry

    key = args.get("key", "").strip()
    if not key:
        return "Please provide a key to resolve."
    try:
        result = await identity_service.resolve_entity(key)
        if not result:
            fallback = person_registry.resolve(key)
            if fallback:
                return f"Identity graph miss — PersonRegistry fallback: {fallback}"
            return f"No identity found for {key!r}"
        verified = "verified" if result["verified"] else "unverified"
        return (
            f"canonical_id: {result['canonical_id']}\n"
            f"confidence: {result['confidence']:.2f} ({verified})\n"
            f"source: {result['source']}"
        )
    except Exception as e:
        log.error("resolve_entity tool failed: %s", e)
        return f"Resolve failed: {e}"


@tool(
    name="get_unresolved_entities",
    description=(
        "List unknown MACs and identifiers seen on the network that couldn't "
        "be matched to any known person or device."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit":     {"type": "integer", "description": "Max results (default 20)"},
            "min_count": {"type": "integer", "description": "Only show entities seen >= this many times (default 1)"},
        },
    },
    role_required=ROLE_ADMIN,
    tier=1,
)
async def handle_get_unresolved_entities(args: dict, ctx) -> str:
    db = ctx.services.db

    limit = args.get("limit", 20)
    min_count = args.get("min_count", 1)
    try:
        rows = await db.list_unresolved_entities(limit=limit, min_count=min_count)
        if not rows:
            return "No unresolved entities — all seen MACs are claimed."
        lines = [f"Unresolved entities ({len(rows)} shown, min seen: {min_count}x):"]
        for r in rows:
            context = json.loads(r["context_snapshot"] or "{}")
            essid = context.get("essid", "")
            essid_str = f" · {essid}" if essid else ""
            lines.append(
                f"• `{r['entity_key']}` [{r['type']}] seen {r['count']}x · "
                f"last: {r['last_seen'][:16]}{essid_str}"
            )
        lines.append("\nTo claim: add MAC to config.json → family_members → device_macs, then /reload.")
        return "\n".join(lines)
    except Exception as e:
        log.error("get_unresolved_entities tool failed: %s", e)
        return f"Failed to fetch unresolved entities: {e}"
