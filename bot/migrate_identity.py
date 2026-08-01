"""
migrate_identity.py — Seed the identity graph from config.json family_members.

Idempotent: safe to call on every /reload. All upsert_* calls are INSERT OR IGNORE
or UPDATE, so repeated runs do not create duplicates.

Usage (standalone):
    python migrate_identity.py

Called automatically by /reload via bot.py → cmd_reload.
"""

import asyncio
import json
import logging
from pathlib import Path

from identity_service import identity_service

log = logging.getLogger(__name__)


async def seed_from_config(config: dict) -> dict:
    """Seed identity graph nodes, aliases, and device edges from config.

    Reads config["family_members"]. For each member:
      - Upserts a person node with metadata (display, first_name, role, email, ha_entity)
      - Upserts aliases: display name, first_name, canonical_id, all entries in aliases[],
        discord_id (if non-zero), ha_entity (if set)
      - For each device MAC: upserts a device node, a MAC alias, and an owned_by edge

    Returns {"seeded": N, "skipped": 0} where N is the number of person nodes written.
    """
    family_members = config.get("family_members", {})
    seeded = 0

    for display_name, member in family_members.items():
        canonical_id = member.get("canonical_id", "")
        if not canonical_id:
            log.warning(f"migrate_identity: skipping {display_name!r} — no canonical_id")
            continue

        first_name = member.get("first_name", display_name)
        role = member.get("role", "")
        email = member.get("email", "")
        ha_entity = member.get("ha_entity", "")
        discord_id = member.get("discord_id", 0)
        aliases = member.get("aliases", [])
        device_macs = member.get("device_macs", [])

        # --- Person node ---
        metadata = {
            "display": display_name,
            "first_name": first_name,
            "role": role,
            "email": email,
            "ha_entity": ha_entity,
        }
        person_node_id = await identity_service.upsert_node(canonical_id, "person", metadata)

        # --- Person aliases ---
        person_aliases = [display_name, first_name, canonical_id] + list(aliases)
        if discord_id and discord_id != 0:
            person_aliases.append(str(discord_id))
        if ha_entity:
            person_aliases.append(ha_entity)

        for alias in person_aliases:
            if alias:
                await identity_service.upsert_alias(
                    alias,
                    person_node_id,
                    confidence=0.95,
                    source="config",
                    verified=True,
                )

        # --- Device nodes + edges ---
        for mac in device_macs:
            mac_lc = mac.lower()
            device_node_id = await identity_service.upsert_node(
                mac_lc,
                "device",
                {"owner": canonical_id, "display": f"{display_name}'s device"},
            )
            await identity_service.upsert_alias(
                mac_lc,
                device_node_id,
                confidence=0.95,
                source="config",
                verified=True,
            )
            await identity_service.upsert_edge(
                device_node_id,
                person_node_id,
                "owned_by",
                confidence=0.95,
                evidence="config.json device_macs",
                verified=True,
            )

        seeded += 1
        log.debug(f"migrate_identity: seeded {canonical_id!r} ({display_name})")

    log.info(f"migrate_identity: seeded {seeded} person nodes from config")
    return {"seeded": seeded, "skipped": 0}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Support both docker path and local path
    config_path = Path("/app/config.json")
    if not config_path.exists():
        config_path = Path(__file__).parent.parent / "config.json"
    
    if config_path.exists():
        config = json.loads(config_path.read_text())
        result = asyncio.run(seed_from_config(config))
        print(result)
    else:
        print(f"Could not find config.json at {config_path}")
