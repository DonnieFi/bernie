"""Gmail read signals + policy-gated send."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone as dt_timezone

from email_pending_delivery import post_email_pending_approval
from email_service import (
    EmailPolicyError,
    EmailRateLimitError,
    check_send_policy,
    get_message,
    send,
)
from tools import ROLE_ALL, ROLE_PARENTS, tool
from tool_utils import strip_markdown
import db_writes


def _snapshot_payload(summary_line: str, core: dict, extras: dict | None) -> str:
    payload = {"summary": summary_line, "core": core, "extras": extras}
    return json.dumps(payload, indent=2)


@tool(
    name="get_recent_email_signals",
    description=(
        "Get recent summarized email signals ingested from the Bernie family inbox "
        "(forwards to the configured Gmail mailbox). Use for questions like school mail, "
        "appointments, or 'anything from X lately'. Returns summaries only, not full bodies."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "person": {
                "type": "string",
                "description": "Optional family member to filter (sender or forwarder).",
            },
            "since_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 90,
                "description": "Look back N days (default 14).",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 25,
                "description": "Max rows (default 10).",
            },
        },
        "required": [],
    },
    role_required=ROLE_ALL,
    domain="email",
    tier=1,
)
async def handle_get_recent_email_signals(args: dict, ctx) -> str:
    db = ctx.services.db
    since_days = int(args.get("since_days") or 14)
    limit = int(args.get("limit") or 10)
    since = (
        datetime.now(dt_timezone.utc) - timedelta(days=since_days)
    ).isoformat().replace("+00:00", "Z")

    person_id = None
    person = (args.get("person") or "").strip()
    if person:
        from constants import registry as person_registry
        person_id = person_registry.resolve(person)
        if not person_id:
            return f"No family member matched '{person}' for email signal filter."

    rows = await db.search_email_signals(person_id=person_id, since_iso=since, limit=limit)
    if not rows:
        hint = f" for {person}" if person else ""
        return f"No recent email signals found{hint} in the last {since_days} day(s)."

    core = []
    for r in rows:
        topics_raw = r.get("topics") or "[]"
        if isinstance(topics_raw, str):
            try:
                topics = json.loads(topics_raw)
            except Exception:
                topics = []
        else:
            topics = topics_raw
        core.append({
            "gmail_id": r.get("gmail_id"),
            "received_at": (r.get("received_at") or "")[:19],
            "subject": r.get("subject") or "",
            "summary": r.get("summary") or "",
            "topics": topics,
            "forwarder_person_id": r.get("forwarder_person_id"),
            "sender_email": r.get("sender_email"),
        })
    extras = {"count": len(rows), "since_days": since_days}
    line = f"{len(rows)} email signal(s) in the last {since_days} day(s)."
    return _snapshot_payload(line, core, extras)


@tool(
    name="read_email_message",
    description=(
        "Fetch the full plain-text body of one ingested email by gmail_id. "
        "Parents/admin only — always audited."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "gmail_id": {"type": "string", "description": "Gmail message id from signals."},
        },
        "required": ["gmail_id"],
    },
    role_required=ROLE_PARENTS,
    domain="email",
    tier=1,
)
async def handle_read_email_message(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would read email {args.get('gmail_id')}]"

    gmail_id = (args.get("gmail_id") or "").strip()
    if not gmail_id:
        return "gmail_id is required."

    db = ctx.services.db
    sig = await db.get_email_signal_by_gmail_id(gmail_id)
    try:
        msg = await get_message(gmail_id)
    except Exception as e:
        return f"Could not fetch message: {e}"

    await db_writes.routed("log_activity", 
        event_type="email_raw_fetch",
        description=f"Raw fetch: {msg.get('subject', '')[:120]}",
        person_id=ctx.person_id,
    )

    header = (
        f"Subject: {msg.get('subject', '')}\n"
        f"From: {msg.get('from_header', '')}\n"
        f"Received: {msg.get('received_at', '')}\n"
    )
    if sig:
        header += f"Summary on file: {sig.get('summary', '')}\n"
    body = (msg.get("body_text") or "").strip()
    return header + "\n" + (body[:12000] if body else "(empty body)")


@tool(
    name="send_email",
    description=(
        "Send an email via Gmail. Family recipients only. Confirm recipient and intent first. "
        "Kids: posts to #smithy for parent approval. Plain text body only."
    ),
    is_write=True,
    input_schema={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient email address"},
            "subject": {"type": "string", "description": "Email subject line"},
            "body": {"type": "string", "description": "Email body text"},
            "cc": {"type": "string", "description": "Optional comma-separated CC addresses"},
            "reply_to_gmail_id": {
                "type": "string",
                "description": "Optional gmail_id when replying on a forwarded thread",
            },
        },
        "required": ["to", "subject", "body"],
    },
    role_required=ROLE_ALL,
    domain="email",
    tier=1,
)
async def handle_send_email(args: dict, ctx) -> str:
    if ctx.shadow:
        return f"[shadow: would have sent email to {args.get('to')!r}]"

    to = (args.get("to") or "").strip()
    subject = (args.get("subject") or "").strip()
    body = strip_markdown(args.get("body") or "")
    cc_raw = (args.get("cc") or "").strip()
    cc = [p.strip() for p in cc_raw.split(",") if p.strip()] if cc_raw else None
    reply_gid = (args.get("reply_to_gmail_id") or "").strip() or None
    thread_id = None
    if reply_gid:
        sig = await ctx.services.db.get_email_signal_by_gmail_id(reply_gid)
        if sig:
            thread_id = sig.get("thread_id")

    action, err = check_send_policy(to, cc, ctx.group, ctx.config)
    if action == "block":
        return err or "Email blocked by policy."

    if action == "approve":
        db = ctx.services.db
        pending_id = await db_writes.routed("create_email_pending", 
            recipient=to,
            subject=subject,
            body=body,
            requester_id=ctx.person_id or "unknown",
            requester_role=ctx.group or "kids",
            cc=cc,
            reply_to_gmail_id=reply_gid,
            thread_id=thread_id,
        )
        router = getattr(ctx.services, "orchestrator", None)
        msg_id = await post_email_pending_approval(
            pending_id=pending_id,
            pending_row=await db.get_email_pending(pending_id),
            config=ctx.config,
            router=router,
        )
        if not msg_id:
            await db_writes.routed("resolve_email_pending", pending_id, status="denied", decided_by="system:no-channel")
            return (
                "Could not post email draft to #smithy (approval channel unavailable). "
                "Nothing was sent."
            )
        await db_writes.routed("log_activity", 
            event_type="email_pending_created",
            description=f"Kid email pending approval to {to}",
            person_id=ctx.person_id,
        )
        return (
            f"Email draft posted to #smithy for parent approval (pending #{pending_id}). "
            "It will not send until a parent reacts ✅."
        )

    try:
        msg_id = await send(
            to,
            subject,
            body,
            cc=cc,
            requester_id=ctx.person_id or "unknown",
            requester_role=ctx.group or "parents",
            config=ctx.config,
            reply_to_gmail_id=reply_gid,
            thread_id=thread_id,
        )
    except EmailPolicyError as e:
        return str(e)
    except EmailRateLimitError as e:
        return str(e)
    except Exception as e:
        log = __import__("logging").getLogger(__name__)
        log.exception("send_email failed")
        return f"Email send failed: {e}"

    cc_note = f" (cc: {', '.join(cc)})" if cc else ""
    return f"Email sent to {to}{cc_note} — subject: {subject} (id: {msg_id})"