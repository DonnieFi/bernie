"""Discovery tools (Phase 39 Wave 1b).

search_tools: keyword scan of the full tool registry so that even on a narrow surface
the model (or user) can find capabilities that are not currently advertised.

Always unioned by name onto the schemas for the turn (see tool_surface).
"""

from __future__ import annotations

from tools import ROLE_ALL, tool


@tool(
    name="search_tools",
    description=(
        "Search the full catalogue of available tools by a keyword or phrase. "
        "Scans tool names, descriptions, and domains. Use this when the active surface "
        "does not include a tool you need (e.g. on a narrow channel or after intent narrowing). "
        "Returns up to 20 matching entries with their descriptions."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword or phrase to match against tool name, description, or domain (case-insensitive)."
            }
        },
        "required": ["query"],
    },
    role_required=ROLE_ALL,
    tier=1,
    domain="search",
)
async def handle_search_tools(args: dict, ctx) -> str:
    q = (args.get("query") or "").strip().lower()
    if not q:
        return "Please provide a query to search tools."

    from tools import get_registry

    reg = get_registry()
    matches: list[str] = []
    for name, entry in sorted(reg.items()):
        hay = " ".join([
            name or "",
            entry.get("description") or "",
            entry.get("domain") or "",
        ]).lower()
        if q in hay:
            desc = entry.get("description") or ""
            dom = entry.get("domain") or ""
            matches.append(f"{name} [{dom}]: {desc}")

    if not matches:
        return f"No tools matched '{q}'."

    # Cap for prompt friendliness
    shown = matches[:20]
    more = len(matches) - len(shown)
    out = "\n".join(shown)
    if more > 0:
        out += f"\n... and {more} more. Refine your query or use list_slash_commands for the full list."
    return out


@tool(
    name="describe_modes",
    description=(
        "Summarize the available modes (concierge, chef, ops, security, etc.), their pinned channels, "
        "and the tool domains they allow/deny. Useful for understanding why certain tools are or are not "
        "visible on the current surface."
    ),
    input_schema={"type": "object", "properties": {}},  # no args
    role_required=ROLE_ALL,
    tier=1,
    domain="search",
)
async def handle_describe_modes(args: dict, ctx) -> str:
    from modes import load_all_modes
    modes = load_all_modes()
    if not modes:
        return "No modes loaded."

    lines = []
    for slug in sorted(modes.keys()):
        m = modes[slug]
        allow = (m.domains or {}).get("allow") or []
        deny = (m.domains or {}).get("deny") or []
        pins = []
        if m.channels:
            pins.append("channels:" + ",".join(m.channels))
        if m.channel_pin:
            pins.append("pinned")
        pin_str = " (" + ";".join(pins) + ")" if pins else ""
        lines.append(
            f"{slug}{pin_str}: allow=[{','.join(allow)}] deny=[{','.join(deny)}]"
        )
    return "Modes:\n" + "\n".join(lines)
