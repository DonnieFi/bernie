class PersonRegistry:
    """Single source of truth for person identity across all subsystems.

    Resolves any identifier (display name, first name, alias, Discord ID,
    HA entity, canonical ID) to a canonical person record.

    Call registry.load(config) once at startup. All lookups go through
    registry.resolve(key) → canonical_id, then registry.get(id) → record.
    """

    def __init__(self):
        self._by_id: dict[str, dict] = {}
        self._lookup: dict[str, str] = {}  # any key (lower) → canonical_id

    def load(self, config: dict):
        self._by_id = {}
        self._lookup = {}
        for display_name, member in config.get("family_members", {}).items():
            cid = member.get("canonical_id", display_name.lower())
            record = {
                "id":           cid,
                "display":      display_name,
                "first_name":   member.get("first_name", display_name),
                "discord_id":   str(member.get("discord_id", "")),
                "ha_entity":    member.get("ha_entity", ""),
                "email":        member.get("email", ""),
                "role":         member.get("role", ""),
                "device_macs":  member.get("device_macs", []),
                "device_ip":    member.get("device_ip"),
                "calendars":    member.get("calendars", []),
            }
            self._by_id[cid] = record

            # Register every alias that should resolve to this person
            keys = set(member.get("aliases", []))
            keys.update([display_name, display_name.lower(), cid])
            for k in keys:
                self._lookup[k.lower()] = cid

            # Discord ID → person
            did = str(member.get("discord_id", ""))
            if did and did != "0":
                self._lookup[did] = cid

            # HA entity → person  (e.g. "person.red" → "dad")
            ha = member.get("ha_entity", "")
            if ha:
                self._lookup[ha.lower()] = cid

    def resolve(self, key) -> str | None:
        """Return the canonical person_id for any identifier, or None."""
        if key is None:
            return None
        return self._lookup.get(str(key).lower()) or self._lookup.get(str(key))

    def get(self, canonical_id: str) -> dict | None:
        """Return the full person record for a canonical_id."""
        return self._by_id.get(canonical_id)

    def all(self) -> list[dict]:
        return list(self._by_id.values())

    def family(self) -> list[dict]:
        """Return all persons except those with role='friend'."""
        return [p for p in self._by_id.values() if p.get("role") != "friend"]

    def all_ids(self) -> list[str]:
        return list(self._by_id.keys())

    def display_name(self, canonical_id: str) -> str:
        r = self._by_id.get(canonical_id)
        return r["display"] if r else canonical_id.capitalize()

    def first_name(self, canonical_id: str) -> str:
        r = self._by_id.get(canonical_id)
        return r["first_name"] if r else canonical_id.capitalize()


# Module-level singleton — call registry.load(config) at bot startup
registry = PersonRegistry()


def resolve_person_from_entity(entity_id: str) -> str | None:
    """
    Given an HA entity like 'person.dad' or 'device_tracker.mom_s_iphone',
    return the canonical person ID (e.g. 'dad'), or None if unknown.
    Delegates to the main PersonRegistry so all services use the same logic.
    """
    if not entity_id:
        return None
    return registry.resolve(entity_id)


def is_family_member(entity_or_id: str) -> bool:
    """
    Returns True if the given entity (person.*, device_tracker.*) or
    canonical_id / alias refers to a known non-friend family member.
    """
    cid = resolve_person_from_entity(entity_or_id) or registry.resolve(entity_or_id)
    if not cid:
        return False
    rec = registry.get(cid)
    return rec is not None and rec.get("role") != "friend"

# Legacy aliases kept for any import sites not yet migrated
# (remove once all callers use registry directly)
PERSON_IDS: dict[str, str] = {}      # display_name → canonical_id
PERSON_DISPLAY: dict[str, str] = {}  # canonical_id → display_name
PERSON_ALIASES: dict[str, str] = {}  # alias → canonical_id


def _rebuild_legacy(config: dict):
    """Sync the legacy dicts from config so old callers still work."""
    PERSON_IDS.clear()
    PERSON_DISPLAY.clear()
    PERSON_ALIASES.clear()
    for display_name, member in config.get("family_members", {}).items():
        cid = member.get("canonical_id", display_name.lower())
        PERSON_IDS[display_name] = cid
        PERSON_DISPLAY[cid] = display_name
        for alias in member.get("aliases", []):
            PERSON_ALIASES[alias.lower()] = cid


# HA entity supported_features bitmask flags
HA_SUPPORT_BRIGHTNESS = 1
HA_SUPPORT_COLOR_TEMP = 2
HA_SUPPORT_RGB_COLOR  = 16
HA_RGB_MODES = {"rgb", "rgbw", "rgbww", "hs", "xy"}
HA_DIM_MODES = HA_RGB_MODES | {"brightness", "color_temp", "white"}

# Tool RBAC role constants (canonical home — tools/__init__.py re-exports these).
ROLE_ALL = "all"
ROLE_PARENTS = "parents"
ROLE_ADMIN = "admin"
ROLE_BERNIE = "bernie"
ROLE_SYSTEM = "system"
