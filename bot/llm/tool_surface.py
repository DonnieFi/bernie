"""Tool surface resolver (Wave 1a).

Surface math only:
- mode ceiling = domains.allow − domains.deny
- channel intersect (with hard #anvil bypass)
- top level resolve_tool_domains (ceiling + channel; narrow + discovery added later)
- startup validation that fails loud on unknown domains or broken mode YAML

This module owns Layer 2 calculations. RBAC remains in ToolGateway.
Discovery union (Layer 3) and caller wiring are out of 1a scope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from modes import ModeDefinition, load_all_modes
from tools import get_registry, load_all_domains

# Note: _channel_is imported locally inside apply_channel_map to avoid pulling
# heavy llm package side-effects at import time (helps host unittests).


def mode_ceiling(mode: ModeDefinition | None) -> list[str] | None:
    """Return (allow − deny) for the mode, or None for 'full surface'."""
    if not mode:
        return None
    dom = mode.domains or {}
    allow = dom.get("allow") or []
    if not allow:
        return None
    deny = set(dom.get("deny") or [])
    ceiling = [d for d in allow if d not in deny]
    return ceiling


def apply_channel_map(
    ceiling: list[str] | None,
    channel_id: str | None,
    config: dict[str, Any],
) -> list[str] | None:
    """Intersect ceiling with channel map if present.

    Hard bypass for #anvil (ops/debug keeps its full surface).
    DMs (no channel_id) never get a channel map applied.
    """
    from .context_legs import _channel_is  # local to avoid top-level llm init side effects

    if _channel_is(config, channel_id, "anvil"):
        return ceiling

    if not channel_id:
        return ceiling

    ch_map: dict = config.get("channel_tool_domains") or {}
    cid = str(channel_id)
    if cid not in ch_map:
        return ceiling

    ch_list = ch_map[cid] or []
    if ceiling is None:
        return list(ch_list)

    ch_set = set(ch_list)
    return [d for d in ceiling if d in ch_set]


def resolve_tool_domains(
    *,
    mode: ModeDefinition | None = None,
    channel_id: str | None = None,
    config: dict[str, Any],
    mode_domains: list[str] | None = None,
    user_message: str = "",
    history: list[dict] | None = None,
    apply_intent_router: bool = True,
) -> list[str] | None:
    """Resolve domains for a turn (Wave 1a full pre-discovery pipeline).

    mode_ceiling (allow − deny) → apply_channel_map (hard anvil bypass, DM-safe) →
    narrow_tool_domains (if enabled and not anvil).

    This is the single entry point for Layer 2 (surface). Discovery union happens later
    (Wave 1b+). Callers that want only the post-channel ceiling can pass apply_intent_router=False.
    """
    if mode is not None:
        try:
            ceiling = mode_ceiling(mode)
        except Exception:
            # Fallback for tests/mocks (e.g. MagicMock ctx.mode without .domains)
            # or any non-standard mode object. Prefer the explicitly passed mode_domains.
            ceiling = mode_domains
    else:
        ceiling = mode_domains
    ch_ceiling = apply_channel_map(ceiling, channel_id, config)

    if not apply_intent_router:
        return ch_ceiling

    # Delegate narrowing (chit-chat strip, core+matched, sticky, etc.) to the intent router.
    # It receives the post-channel ceiling and may return [] or a subset.
    from .intent_router import narrow_tool_domains as _narrow
    return _narrow(
        mode_domains=ch_ceiling,
        user_message=user_message or "",
        config=config,
        history=history,
        channel_id=channel_id,
    )


def validate_tool_surface_at_startup(config: dict[str, Any]) -> None:
    """Fail boot loudly if mode files, channel maps, or discovery list reference unknown domains.

    Also catches unparseable mode YAML (bypassing the tolerant continue in load_all_modes).
    Call after load_all_domains() and load_all_modes() in the startup path.
    """
    load_all_domains()
    modes = load_all_modes()
    registry = get_registry()

    known_domains = {e.get("domain") for e in registry.values() if e.get("domain")}
    known_tools = set(registry.keys())

    # Strict re-parse of mode files to surface YAML/frontmatter errors that load_all_modes swallows.
    modes_dir = Path(__file__).resolve().parent.parent / "modes"
    for md_file in sorted(modes_dir.glob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        try:
            _, frontmatter, _ = text.split("---", 2)
            data = yaml.safe_load(frontmatter) or {}
            dom = data.get("domains") or {}
            if dom and not isinstance(dom, dict):
                raise ValueError("domains must be a mapping")
            for key in ("allow", "deny"):
                for d in (dom.get(key) or []):
                    if d not in known_domains:
                        raise RuntimeError(
                            f"tool_surface: unknown domain {d!r} in {md_file.name} domains.{key}"
                        )
        except Exception as exc:
            raise RuntimeError(f"tool_surface: broken mode file {md_file.name}: {exc}") from exc

    # Validate already-loaded modes (in case load was called before some domains registered).
    for slug, mode in modes.items():
        for key in ("allow", "deny"):
            for d in (mode.domains.get(key) or []):
                if d not in known_domains:
                    raise RuntimeError(
                        f"tool_surface: unknown domain {d!r} referenced in mode {slug!r} domains.{key}"
                    )

    # channel_tool_domains (optional, Wave 3 pilot)
    ch_map = config.get("channel_tool_domains") or {}
    for cid, doms in ch_map.items():
        for d in (doms or []):
            if d not in known_domains:
                raise RuntimeError(
                    f"tool_surface: unknown domain {d!r} in channel_tool_domains for channel {cid!r}"
                )

    # discovery_tools_always_on lists tool *names* (not domains). They must exist once registered.
    ts = config.get("tool_surface") or {}
    for name in (ts.get("discovery_tools_always_on") or []):
        if name not in known_tools:
            raise RuntimeError(
                f"tool_surface: discovery tool {name!r} listed in tool_surface.discovery_tools_always_on but not registered"
            )


def surface_is_narrowed(
    final_domains: list[str] | None,
    reference_ceiling: list[str] | None,
) -> bool:
    """True if final_domains differs from reference_ceiling (subset, superset, or explicit []).

    Used for intent narrowing (final vs post-channel) and channel-map shrink (post-channel vs mode).
    """
    if reference_ceiling is None:
        # Explicit list when no reference ceiling means we have a (possibly narrower) surface.
        return final_domains is not None
    if final_domains is None:
        return False
    return set(final_domains) != set(reference_ceiling)


def turn_surface_narrowed(
    final_domains: list[str] | None,
    post_channel_ceiling: list[str] | None,
    mode_ceiling: list[str] | None,
) -> bool:
    """True when channel map or intent router shrinks the surface vs mode ceiling."""
    if surface_is_narrowed(final_domains, post_channel_ceiling):
        return True
    return surface_is_narrowed(post_channel_ceiling, mode_ceiling)


def append_tool_surface_ux(
    system: list,
    config: dict[str, Any],
    *,
    tool_domains: list[str] | None,
    tool_count: int,
    mode_slug: str | None,
    mode_ceiling: list[str] | None,
    post_channel_ceiling: list[str] | None,
) -> bool:
    """Append active_surface_summary + deferral block when surface is narrowed. Returns narrowed flag."""
    ts = (config or {}).get("tool_surface") or {}
    narrowed = turn_surface_narrowed(tool_domains, post_channel_ceiling, mode_ceiling)
    if not narrowed:
        return False
    if ts.get("inject_active_surface_summary", True):
        disc_note = "Discovery via search_tools / describe_modes / list_slash_commands."
        system.append({
            "type": "text",
            "text": active_surface_summary(
                tool_domains,
                tool_count,
                mode_slug=mode_slug,
                discovery_note=disc_note,
            ),
        })
    if ts.get("inject_deferral_rule", True):
        block = deferral_system_block(config)
        if block:
            system.append({"type": "text", "text": block})
    return narrowed


def get_tool_schemas_for_turn(
    gw,
    group: str | None,
    domains: list[str] | None,
    config: dict[str, Any],  # accepted for union list + future per-tool caps
    cal_available: bool = True,
) -> list[dict]:
    """Return the tool schemas for this turn (domain filtered) with discovery tools unioned by name.

    Discovery tools listed in tool_surface.discovery_tools_always_on (default includes
    search_tools + list_slash_commands) are always added if the caller group can see them,
    even if their domain is not in the current surface. This keeps capabilities discoverable
    on narrow surfaces (e.g. notify-only or chit-chat []).
    Final list is sorted by name for KV-cache stability.
    """
    base = gw.get_tool_schemas(group, cal_available=cal_available, domains=domains)
    base_names = {s["name"] for s in base}

    ts = (config or {}).get("tool_surface") or {}
    always = ts.get("discovery_tools_always_on") or ["search_tools", "list_slash_commands", "describe_modes"]  # Wave 2 addition (describe_modes)

    if not always:
        return base

    # Get the full allowed surface for the group so we can pull the discovery entries
    # (they will have already passed RBAC + cal filter inside the gateway).
    full = gw.get_tool_schemas(group, cal_available=cal_available, domains=None)

    additions = []
    for s in full:
        if s["name"] in always and s["name"] not in base_names:
            additions.append(s)

    if not additions:
        return base

    final = base + additions
    final.sort(key=lambda t: t["name"])
    return final


def active_surface_summary(
    domains: list[str] | None,
    tool_count: int,
    *,
    mode_slug: str | None = None,
    discovery_note: str | None = None,
) -> str:
    """Rich active surface summary for injection (Wave 2).

    Includes mode slug when known, tool count, domain list, and optional discovery line.
    Falls back gracefully for old call sites (domains, count).
    """
    if domains is not None and len(domains) == 0:
        base = (
            "Active tool surface: none (conversational reply only). "
            "If the user needs data or actions, they can ask explicitly."
        )
    else:
        d = ", ".join(domains) if domains else "all permitted"
        prefix = f"Active tool surface (mode: {mode_slug}): " if mode_slug else "Active tool surface: "
        base = f"{prefix}{tool_count} tools from domains [{d}]."

    if discovery_note:
        base = f"{base} {discovery_note}"
    elif domains is not None and len(domains) > 0:
        # Default discovery hint when we know the surface is limited
        base = f"{base} Use search_tools to find more."

    return base


def deferral_system_block(config: dict[str, Any]) -> str:
    """Fixed short block for family chat paths when a turn is on a narrowed surface.

    Injected dynamically (not in mode markdowns). Mentions real family channels + search_tools.
    Wave 2 item; a one-liner version was added optionally in 1b.
    """
    if not (config or {}).get("tool_surface", {}).get("inject_deferral_rule", True):
        return ""
    # Conservative family-facing copy (no jargon). Named channels per household reality.
    return (
        "If what you need isn't listed right now, say the name or use search_tools. "
        "Try #smithy for most things, #furnace for meals, or / commands. "
        "Admins: #anvil has the full set."
    )
