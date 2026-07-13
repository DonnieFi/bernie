"""Mode loader and resolver for Phase 28 Wave 2c.

Each mode is defined in bot/modes/<slug>.md (YAML frontmatter + prompt body).
This module provides:
- load_all_modes()
- resolve_mode(...) with full precedence logic (including #anvil special case)
- set_mode_override() / get_mode_override() for the switch_mode tool
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ModeDefinition:
    slug: str
    name: str
    visibility: str = "primary"
    channels: list[str] = field(default_factory=list)
    channel_pin: dict[str, Any] = field(default_factory=dict)
    triggers: dict[str, Any] = field(default_factory=dict)
    domains: dict[str, Any] = field(default_factory=dict)
    model_preference: dict[str, Any] = field(default_factory=dict)
    prompt_addendum: str = ""


_modes: dict[str, ModeDefinition] = {}
_mode_override: str | None = None


def load_all_modes() -> dict[str, ModeDefinition]:
    """Load all mode definitions from bot/modes/*.md (idempotent after first call)."""
    global _modes
    if _modes:
        return _modes

    modes_dir = Path(__file__).parent
    for md_file in sorted(modes_dir.glob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8")
            if not text.startswith("---"):
                continue
            _, frontmatter, body = text.split("---", 2)
            data = yaml.safe_load(frontmatter) or {}

            mode = ModeDefinition(
                slug=data.get("slug", md_file.stem),
                name=data.get("name", md_file.stem.replace("-", " ").title()),
                visibility=data.get("visibility", "primary"),
                channels=data.get("channels", []),
                channel_pin=data.get("channel_pin") or {},
                triggers=data.get("triggers", {}),
                domains=data.get("domains", {}),
                model_preference=data.get("model_preference", {}),
                prompt_addendum=body.strip(),
            )
            _modes[mode.slug] = mode
        except Exception:
            continue  # never crash the bot on a bad mode file

    return _modes


def get_mode(slug: str) -> ModeDefinition | None:
    if not _modes:
        load_all_modes()
    return _modes.get(slug)


def set_mode_override(slug: str | None) -> None:
    """Set an explicit mode override (used by the switch_mode tool)."""
    global _mode_override
    _mode_override = slug


def get_mode_override() -> str | None:
    return _mode_override


def _matches_keywords(text: str | None, keywords: list[str]) -> bool:
    if not text or not keywords:
        return False
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def _config_value_at_path(cfg: dict, path: str) -> str:
    parts = path.split(".")
    cur: Any = cfg
    for part in parts:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(part, "")
    return str(cur or "")


def _channel_matches_pin(channel_id: str | None, cfg: dict, pin: dict) -> bool:
    if not channel_id or not pin:
        return False
    ch_id = str(channel_id)
    keys: list[str] = []
    single = pin.get("config_channel_key")
    if single:
        keys.append(single)
    keys.extend(pin.get("config_channel_keys") or [])
    for key in keys:
        if ch_id == str(cfg.get(key, "")):
            return True
    for path in pin.get("nested_config_paths") or []:
        if ch_id == _config_value_at_path(cfg, path):
            return True
    return False


def _resolve_channel_pin(
    channel: str | None,
    message_text: str | None,
    cfg: dict,
) -> ModeDefinition | None:
    """Declarative channel → mode mapping from mode frontmatter channel_pin blocks."""
    for mode in _modes.values():
        pin = mode.channel_pin or {}
        if not pin or not _channel_matches_pin(channel, cfg, pin):
            continue
        for override in pin.get("keyword_overrides") or []:
            target = override.get("mode") or override.get("slug")
            keywords = override.get("keywords") or []
            if target and target in _modes and _matches_keywords(message_text, keywords):
                return _modes[target]
        return mode
    return None


def resolve_mode(
    *,
    channel: str | None = None,
    person_id: str | None = None,
    message_text: str | None = None,
    quiet_hours_active: bool = False,
    explicit_override: str | None = None,
    openwebui: bool = False,
) -> ModeDefinition:
    """
    Resolve the active mode using documented precedence.

    Precedence (highest first):
    1. Explicit override (switch_mode tool or /mode)
    2. OpenWebUI direct chat (chat-openwebui) — forced highest priority for web interface
    3. Event-driven (frigate_alert / away_state_change → security)
    4. Channel-pinned (#furnace → chef, #anvil → ops or debug)
    5. Actor + keyword
    6. Quiet hours → wind-down
    7. Default → concierge

    #anvil special rule: default = ops, keyword "debug" → debug
    """
    if not _modes:
        load_all_modes()

    # 1. Explicit override wins immediately
    override = explicit_override or get_mode_override()
    if override and override in _modes:
        return _modes[override]

    # 2. OpenWebUI direct chat — highest priority for the web interface path
    if openwebui and "chat-openwebui" in _modes:
        return _modes["chat-openwebui"]

    # 3. Event-driven modes
    if message_text and ("frigate" in message_text.lower() or "alert" in message_text.lower()):
        if "security" in _modes:
            return _modes["security"]

    # 4. Channel pins (declarative via mode frontmatter channel_pin)
    from config import config

    pinned = _resolve_channel_pin(channel, message_text, config)
    if pinned is not None:
        return pinned

    # 5. Actor + keyword (must match BOTH if the mode declares actors)
    for mode in _modes.values():
        trig = mode.triggers or {}
        actors = [a.lower() for a in trig.get("actors", [])]
        keywords = trig.get("keywords", [])

        actor_match = bool(person_id and person_id.lower() in actors)
        keyword_match = _matches_keywords(message_text, keywords)

        if actors:
            # For modes like tutor that list specific actors, require BOTH
            if actor_match and keyword_match:
                return mode
        else:
            # Modes without actor restrictions can trigger on keywords alone
            if keyword_match and mode.slug not in ("security", "wind-down"):
                return mode

    # 6. Quiet hours
    if quiet_hours_active and "wind-down" in _modes:
        return _modes["wind-down"]

    # 7. Default
    return _modes.get("concierge") or next(iter(_modes.values()))
