"""Pure task visibility helpers — no I/O, unit-testable."""


def registry_person_id(stored: str | None) -> str | None:
    """Normalize stored ids for person_registry lookups; agents pass through unchanged."""
    if not stored:
        return None
    raw = str(stored).strip()
    if raw.startswith("agent:"):
        return raw
    bare = raw[7:] if raw.startswith("person:") else raw
    return bare.lower()


def person_id_db_forms(person_id: str) -> tuple[str, str]:
    """Both storage shapes (bare + person: prefix) for SQL IN matching."""
    canon = registry_person_id(person_id)
    if not canon:
        return ("", "")
    if canon.startswith("agent:"):
        return (canon, canon)
    return (canon, f"person:{canon}")


def person_matches(stored: str | None, user_id: str) -> bool:
    """True when a stored assignee/assigner id refers to the same person as user_id."""
    if not stored or not user_id:
        return False
    a = registry_person_id(stored)
    b = registry_person_id(user_id)
    return bool(a and b and a == b)


def can_view_task(task: dict, user_id: str, user_role: str) -> bool:
    """True when user may read detail/comment on a task."""
    if task.get("visibility") == "internal" and user_role not in {"admin", "parents"}:
        return False
    if user_role in {"admin", "parents"}:
        return True
    if person_matches(task.get("assigned_to"), user_id):
        return True
    if person_matches(task.get("assigned_by"), user_id):
        return True
    for aid in task.get("acceptable_assignees") or []:
        if person_matches(aid, user_id):
            return True
    return False


def person_to_discord_id(person_id: str | None) -> int | None:
    """Resolve a canonical person ID or alias to their Discord ID (int)."""
    from constants import registry as person_registry
    if not person_id:
        return None
    raw = str(person_id).strip()
    canonical = person_registry.resolve(raw) or registry_person_id(raw)
    person = person_registry.get(canonical or "")
    if person:
        did = person.get("discord_id")
        return int(did) if did and str(did) != "0" else None
    return None
