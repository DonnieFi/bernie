"""Family / person context (memory file) tool handlers."""
from __future__ import annotations

import pathlib

from tools import ROLE_ALL, ROLE_PARENTS, tool


def _docs_root() -> pathlib.Path:
    from config import DOCS_ROOT
    return pathlib.Path(DOCS_ROOT)


def _cfg() -> dict:
    from config import config
    return config


@tool(
    name="read_family_context",
    description=(
        "Read stable facts about the household — routines, "
        "preferences, recurring patterns."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_read_family_context(args: dict, ctx) -> str:
    p = _docs_root() / "context.md"
    return p.read_text().strip() if p.exists() else "No family context file found."


@tool(
    name="update_family_context",
    description=(
        "Append a new stable fact to the family context file when you learn "
        "something that should be remembered permanently. "
        "Cannot write USER_OVERRIDE.md (immutable family facts)."
    ),
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "One clear sentence to remember."},
        },
        "required": ["fact"],
    },
    role_required=ROLE_PARENTS,
    tier=2,
)
async def handle_update_family_context(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called update_family_context({args})]"
    from memory_docs import (
        append_fact_with_cap,
        context_max_chars,
        is_override_path,
        maybe_warn_anvil,
    )

    root = _docs_root()
    p = root / "context.md"
    if is_override_path(p, root, _cfg()):
        return "Refused: cannot write immutable USER_OVERRIDE via this tool."
    fact = args["fact"].replace("\n", " ").strip()[:500]
    msg, consolidated = append_fact_with_cap(
        p, fact, max_chars=context_max_chars(_cfg())
    )
    if consolidated:
        await maybe_warn_anvil(ctx, f"context.md consolidated ({msg})")
    return msg


@tool(
    name="read_person_context",
    description=(
        "Read a person-specific context file (e.g., dad.md, mom.md, "
        "child1.md) to learn preferences and patterns about that individual."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "Person's name (e.g., 'Dad')"},
        },
        "required": ["person"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_read_person_context(args: dict, ctx) -> str:
    from constants import registry as person_registry
    person_name = args["person"]
    resolved_name = person_registry.resolve(person_name)
    if not resolved_name:
        return f"Could not identify person '{person_name}'."
    p = _docs_root() / f"{resolved_name}.md"
    return p.read_text().strip() if p.exists() else f"No context file for {person_name}."


@tool(
    name="update_person_context",
    description=(
        "Append a fact to a person-specific context file to remember "
        "preferences, habits, or corrections. "
        "Cannot write USER_OVERRIDE.md (immutable family facts)."
    ),
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "Person's name"},
            "fact":   {"type": "string", "description": "One clear sentence to remember."},
        },
        "required": ["person", "fact"],
    },
    role_required=ROLE_PARENTS,
    tier=2,
)
async def handle_update_person_context(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called update_person_context({args})]"
    from constants import registry as person_registry
    from memory_docs import (
        append_fact_with_cap,
        is_override_path,
        maybe_warn_anvil,
        person_max_chars,
    )

    person_name = args["person"]
    resolved_name = person_registry.resolve(person_name)
    if not resolved_name:
        return f"Could not identify person '{person_name}'."
    root = _docs_root()
    p = root / f"{resolved_name}.md"
    if is_override_path(p, root, _cfg()):
        return "Refused: cannot write immutable USER_OVERRIDE via this tool."
    # Never treat person slug as override filename
    if p.name.upper() in {"USER_OVERRIDE.MD", "DAD_OVERRIDE.MD"}:
        return "Refused: USER_OVERRIDE.md is immutable — edit it only as a human on disk."
    fact = args["fact"].replace("\n", " ").strip()[:500]
    msg, consolidated = append_fact_with_cap(
        p, fact, max_chars=person_max_chars(_cfg())
    )
    if consolidated:
        await maybe_warn_anvil(ctx, f"{p.name} consolidated ({msg})")
    return f"{msg} ({person_name})"


@tool(
    name="read_user_override",
    description=(
        "Read immutable family facts from USER_OVERRIDE.md (human-edited only). "
        "These facts always win over agent-written context."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_read_user_override(args: dict, ctx) -> str:
    from memory_docs import read_user_override

    text = read_user_override(_docs_root(), _cfg())
    return text if text else "No USER_OVERRIDE.md found (optional human-edited facts file)."

@tool(
    name="search_activity_log",
    description=(
        "Search Bernie's own activity log (everything he has ever logged) using "
        "full-text search. Returns the most relevant past events, tool calls, "
        "observations, and actions. Use this for self-reflection, debugging, "
        "or understanding patterns. Supports simple keyword and phrase queries."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language or FTS5 query (e.g. 'frigate person', 'grocery list', '\"child1\" NEAR homework')",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Maximum number of results to return (default 15)",
            },
            "since_days": {
                "type": "integer",
                "minimum": 1,
                "description": "Only search the last N days (optional)",
            },
            "person_id": {
                "type": "string",
                "description": "Filter to events involving a specific person (optional)",
            },
        },
        "required": ["query"],
    },
    role_required=ROLE_PARENTS,   # Powerful introspection — further restricted by modes
    domain="admin",
    tier=1,
)
async def handle_search_activity_log(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have searched activity log for: {args.get('query')} ]"

    db = ctx.services.db

    query = args["query"].strip()
    limit = int(args.get("limit", 15))
    since_days = args.get("since_days")
    person_id = args.get("person_id")

    try:
        hits = await db.search_activity_log(
            query=query,
            limit=limit,
            since_days=since_days,
            person_id=person_id,
        )
    except Exception as e:
        return f"Search failed: {e}"

    if not hits:
        return f"No activity found matching “{query}”."

    lines = []
    for h in hits:
        time_str = h.get("time", "")[:19].replace("T", " ")
        who = h.get("actor") or h.get("person_id") or "system"
        chan = h.get("channel") or ""
        chan_str = f"#{chan}" if chan else ""
        snippet = (h.get("description") or "")[:180].replace("\n", " ")
        lines.append(f"• [{time_str}] {who} {chan_str}: {snippet}")

    return "\n".join(lines)


@tool(
    name="session_search",
    description=(
        "Search family chat transcripts (conversation_history) via FTS5. "
        "Modes: discover (full-text query), scroll (page by id cursor), "
        "browse (window around a message id or latest in a channel). "
        "Complements search_activity_log (events) — this is chat content. "
        "discover hard-caps at 50 hits (not the admin list limit of 100)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["discover", "scroll", "browse"],
                "description": "discover=FTS query; scroll=paginate; browse=around id/latest",
            },
            "query": {
                "type": "string",
                "description": "FTS5 query for discover mode (e.g. 'soccer practice', '\"child1\" NEAR exam')",
            },
            "channel_id": {
                "type": "integer",
                "description": "Optional Discord channel id filter",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Max rows (default 15)",
            },
            "since_days": {
                "type": "integer",
                "minimum": 1,
                "description": "discover only: restrict to last N days",
            },
            "before_id": {
                "type": "integer",
                "description": "scroll: page older than this message id",
            },
            "after_id": {
                "type": "integer",
                "description": "scroll: page newer than this message id",
            },
            "around_id": {
                "type": "integer",
                "description": "browse: center window on this message id",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "discover: result offset for paging",
            },
        },
        "required": ["mode"],
    },
    role_required=ROLE_PARENTS,
    domain="admin",
    tier=1,
)
async def handle_session_search(args: dict, ctx) -> str:
    """5hy.11 Hermes U2 session_search."""
    if ctx.shadow:
        return f"[shadow: session_search mode={args.get('mode')} query={args.get('query')!r}]"

    db = ctx.services.db
    mode = (args.get("mode") or "discover").strip().lower()
    limit = int(args.get("limit", 15))
    channel_id = args.get("channel_id")
    try:
        if mode == "discover":
            query = (args.get("query") or "").strip()
            if not query:
                return "discover mode requires a query."
            hits = await db.search_conversation_history(
                query,
                limit=limit,
                channel_id=channel_id,
                since_days=args.get("since_days"),
                offset=int(args.get("offset") or 0),
            )
        elif mode == "scroll":
            hits = await db.scroll_conversation_history(
                channel_id=channel_id,
                before_id=args.get("before_id"),
                after_id=args.get("after_id"),
                limit=limit,
            )
        elif mode == "browse":
            hits = await db.browse_conversation_history(
                around_id=args.get("around_id"),
                channel_id=channel_id,
                limit=limit,
            )
        else:
            return f"Unknown mode {mode!r}; use discover|scroll|browse."
    except Exception as e:
        return f"session_search failed: {e}"

    if not hits:
        return f"No conversation rows for mode={mode}."

    lines = [f"session_search ({mode}) — {len(hits)} hit(s):"]
    for h in hits:
        ts = (h.get("created_at") or "")[:19].replace("T", " ")
        role = h.get("role") or "?"
        snip = (h.get("snippet") or h.get("content") or "")[:160].replace("\n", " ")
        lines.append(f"• #{h.get('id')} ch={h.get('channel_id')} [{ts}] {role}: {snip}")
    return "\n".join(lines)
