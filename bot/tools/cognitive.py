"""Cognitive / deferred-work tool handlers."""
from __future__ import annotations

import logging

from tools import ROLE_ALL, tool
import db_writes

log = logging.getLogger(__name__)


@tool(
    name="ask_ollama",
    description=(
        "Delegate a background task, complex summarization, or detailed research "
        "query to the Ollama model running on Deba (remote LAN). Use for low-stakes "
        "but high-context work to save frontier model tokens. Will fail if Deba is "
        "unreachable."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query":         {"type": "string", "description": "Specific question or task"},
            "system_prompt": {"type": "string", "description": "Optional custom system instructions"},
            "model_alias":   {"type": "string", "description": "Optional alias from config 'ollama_model_aliases'"},
        },
        "required": ["query"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_ask_ollama(args: dict, ctx) -> str:
    from llm.ollama import call_ollama

    query = args["query"]
    system = args.get("system_prompt", "You are Bernie's subconscious, a helpful local assistant.")
    messages = [{"role": "user", "content": query}]

    model_override = None
    alias = args.get("model_alias")
    if alias:
        model_override = ctx.config.get("ollama_model_aliases", {}).get(alias)
        if not model_override:
            log.warning("ask_ollama: unknown alias '%s', using default", alias)
    try:
        return await call_ollama(
            system, messages, ctx.config, ctx.services.session, model_override=model_override
        )
    except Exception as e:
        return f"Ollama execution failed: {e}"


@tool(
    name="defer_response",
    description=(
        "Acknowledgement signal — call this immediately after `request_research` "
        "(or any other long-running tool) so the user gets a 'working on it' "
        "reply right away while the background worker finishes. The returned "
        "string is the acknowledgement text; use it verbatim in your reply. "
        "Do NOT call this for questions you can answer directly — only when a "
        "background task is already queued."
    ),
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "topic":           {"type": "string", "description": "Short summary of what the background task will do (for logging)."},
            "acknowledgement": {"type": "string", "description": "Immediate reply for the user, e.g. 'On it — I'll DM you the results in a minute.'"},
        },
        "required": ["topic", "acknowledgement"],
    },
    role_required=ROLE_ALL,
    tier=2,
)
async def handle_defer_response(args: dict, ctx) -> str:
    """Acknowledgement-only signal — no background task is spawned.

    Historical context: this used to enqueue a `discord_reply` cognitive task
    that handed `topic` to phi4 to "answer in the background". In practice
    phi4 (no tools, no fetched context) just produced "I'm unable to research
    or send emails" hallucinations and DM'd them to the user, racing with
    the real result from `request_research`. Removing the task entirely.
    The tool stays so existing system-prompt instructions still work — it
    just returns the acknowledgement string for the model to echo.
    """
    if ctx.shadow:
        return f"[shadow: would have called defer_response({args})]"

    topic = args.get("topic", "")
    ack = args.get("acknowledgement", "On it — I'll get back to you.")
    if not topic:
        return "defer_response: topic is required."

    log.info(
        "defer_response: ack-only (no background task) actor=%s topic=%r",
        ctx.person_id, topic[:60],
    )
    return ack


@tool(
    name="request_research",
    description=(
        "Spawn an asynchronous research task on Ollama/deba (ResearchWorker). "
        "Use for trip planning, multi-source comparisons, and deep dives. "
        "The requester gets the result as a DM by default, or by email if "
        "`delivery='email'`. Always call defer_response immediately after. "
        "Do NOT answer these inline with web_search or Claude synthesis."
    ),
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "topic":        {"type": "string", "description": "Full research question"},
            "depth":        {"type": "integer", "minimum": 1, "maximum": 3,
                             "description": "Iteration depth — 1 quick, 2 default, 3 deep"},
            "requester_id": {"type": "string",  "description": "Discord ID of the recipient (defaults to caller)"},
            "delivery":     {"type": "string",  "enum": ["dm", "email"],
                             "description": "How to deliver the result. Default 'dm'. Use 'email' when the user explicitly asks."},
            "email":        {"type": "string",  "description": "Override email address. Omit to use the recipient's registered email."},
            "unified_task_id": {"type": "integer",
                                 "description": "Continue research on an existing board task id (optional)."},
        },
        "required": ["topic"],
    },
    role_required=ROLE_ALL,
    tier=2,
)
async def handle_request_research(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called request_research({args})]"
    db = ctx.services.db
    from constants import registry as person_registry
    import research_delivery_queue as _queue

    actor_id = ctx.person_id
    topic = (args.get("topic") or "").strip()
    depth = int(args.get("depth", 2) or 2)
    delivery = (args.get("delivery") or "dm").lower()
    if delivery not in ("dm", "email"):
        delivery = "dm"

    # Resolve requester_id to a Discord snowflake. Models frequently misread the
    # `requester_id` slot ("DM result to user X") as a free-form recipient and
    # pass an email, a name, or the user's display alias. Discord snowflakes
    # are all-digit strings (17-19 chars). Anything else → fall back to the
    # caller's resolved discord_id so the deliver step doesn't silently fail
    # against a bogus recipient.
    explicit_requester = args.get("requester_id")
    requester_snowflake = ""
    if explicit_requester:
        candidate = str(explicit_requester).strip()
        if candidate.isdigit():
            requester_snowflake = candidate
        else:
            log.warning(
                "request_research: explicit requester_id %r is not a Discord snowflake — "
                "falling back to caller",
                candidate[:80],
            )
    if not requester_snowflake:
        person_id = person_registry.resolve(actor_id) if actor_id else None
        person = person_registry.get(person_id) if person_id else None
        requester_snowflake = str(person.get("discord_id", "")) if person else ""

    # Resolve email for the requester. Explicit override wins; otherwise look it
    # up from the person registry via the resolved discord snowflake.
    email = (args.get("email") or "").strip()
    if not email:
        recipient_pid = person_registry.resolve(requester_snowflake)
        recipient = person_registry.get(recipient_pid) if recipient_pid else None
        email = (recipient.get("email") if recipient else "") or ""

    if not topic:
        return "Cannot start research without a topic."

    unified_task_id = args.get("unified_task_id")
    if unified_task_id is not None:
        try:
            uid = int(unified_task_id)
            task_row = await db.get_task(uid)
            if not task_row or task_row.get("type") != "research":
                return f"Task #{uid} is not a research task."
            from research_bridge import enqueue_for_unified

            await enqueue_for_unified(uid, topic, actor_id=actor_id or "", task_store=db)
            return f"Continued research on board task #{uid}."
        except (TypeError, ValueError):
            return "Invalid unified_task_id."
        except Exception as e:
            log.error("request_research: unified enqueue failed: %s", e)
            return f"Could not continue research on that task ({e})."

    if not requester_snowflake:
        return "Cannot start research — could not resolve requester Discord ID."
    if delivery == "email" and not email:
        return ("Cannot start research with delivery='email' — no email on file for the "
                "recipient. Pass an explicit `email` field or use delivery='dm'.")
    try:
        tid = await db_writes.routed("create_cognitive_task", 
            type="research",
            payload={
                "topic": topic,
                "depth": depth,
                "requester_id": requester_snowflake,
                "delivery": delivery,
                "email": email,
            },
            actor_id=actor_id or "",
            channel_id=requester_snowflake,
            priority=5,
        )
        log.info(
            "request_research: enqueued task #%d topic=%r depth=%d delivery=%s",
            tid, topic[:80], depth, delivery,
        )
        # Register so bot.py can react 💬/✉️ on Bernie's reply, letting the user
        # flip the choice before delivery runs.
        _queue.register(requester_snowflake, tid, topic, default_delivery=delivery)
        method_label = "email" if delivery == "email" else "DM"
        return (
            f"Research task #{tid} queued. The requester will receive the result via {method_label}. "
            "They can react 💬 (DM) or ✉️ (email) on the reply to change the method."
        )
    except Exception as e:
        log.error("request_research: enqueue failed: %s", e)
        return f"Sorry, I couldn't schedule that research task ({e})."


@tool(
    name="get_research_thread",
    description="Read prior research memory for a unified board research task (findings, notes).",
    input_schema={
        "type": "object",
        "properties": {
            "unified_task_id": {"type": "integer", "description": "Unified task id (type=research)"},
            "title": {"type": "string", "description": "Optional title search if id unknown"},
        },
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_get_research_thread(args: dict, ctx) -> str:
    db = ctx.services.db
    uid = args.get("unified_task_id")
    if uid is None and args.get("title"):
        rows = await db.find_research_tasks_by_title(args.get("title") or "")
        if not rows:
            return "No matching research task found."
        if len(rows) > 1:
            titles = ", ".join(f"#{r['id']} {r['title']}" for r in rows)
            return f"Multiple matches — pass unified_task_id. Candidates: {titles}"
        uid = rows[0]["id"]
    if uid is None:
        return "Provide unified_task_id or title."
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return "Invalid unified_task_id."
    task = await db.get_task(uid)
    if not task or task.get("type") != "research":
        return f"Task #{uid} is not a research task."
    entries = await db.list_research_memory(uid)
    if not entries:
        return f"No research memory yet for task #{uid} ({task.get('title', '')})."
    import json

    return json.dumps({"task_id": uid, "title": task.get("title"), "entries": entries[-15:]}, indent=2)


@tool(
    name="append_research_thread_note",
    description="Append a note to a unified research task thread (preference, question, decision, etc.).",
    input_schema={
        "type": "object",
        "properties": {
            "unified_task_id": {"type": "integer"},
            "kind": {
                "type": "string",
                "enum": ["finding", "preference", "rejected", "question", "decision", "note"],
            },
            "content": {"type": "string"},
        },
        "required": ["unified_task_id", "content"],
    },
    role_required=ROLE_ALL,
    is_write=True,
    tier=2,
)
async def handle_append_research_thread_note(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have called append_research_thread_note({args})]"
    db = ctx.services.db
    uid = int(args["unified_task_id"])
    task = await db.get_task(uid)
    if not task or task.get("type") != "research":
        return f"Task #{uid} is not a research task."
    kind = (args.get("kind") or "note").strip()
    content = (args.get("content") or "").strip()
    if not content:
        return "content is required."
    await db.append_research_memory(uid, kind, content)
    return f"Appended {kind} note to research task #{uid}."
