"""family-bot-co8: config-driven web UI favorites/names (no Example hardcodes)."""
from __future__ import annotations

from typing import Any


def _light_slug(entity_id: str) -> str:
    """light.dad_lamp → dad-lamp (matches /api/lights/{id})."""
    eid = (entity_id or "").strip()
    if eid.startswith("light."):
        eid = eid[6:]
    return eid.replace("_", "-")


def web_ui_prefs(config: dict | None) -> dict[str, Any]:
    """Build family-shell prefs from config.json web + HA entity flags."""
    cfg = config or {}
    web = cfg.get("web") if isinstance(cfg.get("web"), dict) else {}
    ha = cfg.get("home_assistant") if isinstance(cfg.get("home_assistant"), dict) else {}
    ents = ha.get("entities") if isinstance(ha.get("entities"), list) else []

    # Explicit list of light entity ids or slugs
    raw_quick = web.get("quick_lights") if isinstance(web.get("quick_lights"), list) else []
    slugs: list[str] = []
    for item in raw_quick:
        if not item:
            continue
        slugs.append(_light_slug(str(item)))

    # Or entities marked favorite / quick_action
    if not slugs:
        for e in ents:
            if not isinstance(e, dict):
                continue
            if not (e.get("quick_action") or e.get("favorite")):
                continue
            eid = str(e.get("entity_id") or "")
            if not eid.startswith("light."):
                continue
            slugs.append(_light_slug(eid))

    # Resolve display names from entity config
    by_slug: dict[str, str] = {}
    for e in ents:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("entity_id") or "")
        if not eid.startswith("light."):
            continue
        by_slug[_light_slug(eid)] = str(e.get("name") or eid)

    quick_lights = [
        {"id": s, "name": by_slug.get(s) or s.replace("-", " ").title()}
        for s in slugs
        if s
    ]

    rooms = web.get("air_quality_rooms")
    if not isinstance(rooms, list):
        rooms = None  # JS shows all climate rooms
    else:
        rooms = [str(r) for r in rooms if r]

    colors = web.get("person_colors") if isinstance(web.get("person_colors"), dict) else {}

    return {
        "family_name": cfg.get("family_name") or "Family",
        "quick_lights": quick_lights,
        "air_quality_rooms": rooms,
        "person_colors": {str(k): str(v) for k, v in colors.items()},
    }
