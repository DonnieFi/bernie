"""
Gmail read + send for Bernie's mailbox (gmail_token.json).

All outbound mail MUST go through ``send()`` — policy, rate limits, reply routing,
and hygiene run there. Ingest workers call list/get helpers directly (read-only).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from email.mime.text import MIMEText
from email.utils import parseaddr
from typing import Literal

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import db_writes

log = logging.getLogger(__name__)

_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})

GMAIL_TOKEN_FILE = __import__("os").environ.get("GMAIL_TOKEN_FILE", "/credentials/gmail_token.json")
GMAIL_CREDENTIALS_FILE = __import__("os").environ.get("GOOGLE_CREDENTIALS_FILE", "/credentials/credentials.json")
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

PolicyAction = Literal["allow", "approve", "block"]


class EmailPolicyError(Exception):
    """Recipient or role blocked by send policy."""


class EmailRateLimitError(Exception):
    """Hourly send cap exceeded."""


class GmailNotConfiguredError(FileNotFoundError):
    """Raised when gmail_token.json is missing."""


def gmail_is_configured() -> bool:
    return os.path.exists(GMAIL_TOKEN_FILE)


def _get_gmail_service():
    if not gmail_is_configured():
        raise GmailNotConfiguredError(f"Gmail token not found at {GMAIL_TOKEN_FILE}")
    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def normalize_email(addr: str) -> str:
    """Lowercase + parseaddr; Gmail addresses also strip dots and +aliases."""
    _, email = parseaddr(addr.strip())
    email = (email or addr.strip()).lower()
    if "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    if domain in _GMAIL_DOMAINS:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def _normalize_email(addr: str) -> str:
    return normalize_email(addr)


def resolve_family_cc_email(config: dict, config_key: str) -> str | None:
    """Return a CC address that passes family send policy."""
    family = family_email_set(config)
    explicit = (config.get(config_key) or "").strip()
    if explicit and normalize_email(explicit) in family:
        return explicit
    for member in (config.get("family_members") or {}).values():
        if not isinstance(member, dict):
            continue
        if member.get("role") in ("admin", "parents"):
            em = (member.get("email") or "").strip()
            if em and normalize_email(em) in family:
                return em
    return None


def family_email_set(config: dict) -> set[str]:
    emails: set[str] = set()
    for member in (config.get("family_members") or {}).values():
        if isinstance(member, dict):
            raw = (member.get("email") or "").strip()
            if raw:
                emails.add(_normalize_email(raw))
    return emails


def _parse_cc(cc: str | list[str] | None) -> list[str]:
    if not cc:
        return []
    if isinstance(cc, str):
        parts = [p.strip() for p in cc.split(",") if p.strip()]
    else:
        parts = [str(p).strip() for p in cc if str(p).strip()]
    return [_normalize_email(p) for p in parts]


def check_send_policy(
    to: str,
    cc: str | list[str] | None,
    requester_role: str | None,
    config: dict,
) -> tuple[PolicyAction, str | None]:
    """Return (action, error_message). error_message set only for block."""
    family = family_email_set(config)
    addresses = [_normalize_email(to)] + _parse_cc(cc)
    for addr in addresses:
        if not addr:
            continue
        if addr not in family:
            return "block", f"Email blocked: address '{addr}' is not a family address."

    role = (requester_role or "").lower()
    if role == "kids":
        return "approve", None
    if role in ("admin", "parents", "system"):
        return "allow", None
    # Friends / unknown roles require parent approval.
    return "approve", None


def _email_config(config: dict) -> dict:
    return config.get("email") or {}


async def _notify_anvil_rate_limit(config: dict, message: str) -> None:
    anvil_id = config.get("anvil_channel_id")
    if not anvil_id:
        return
    try:
        from llm.model_state import get_container

        container = get_container()
        orch = container.notification_orchestrator if container else None
        if orch:
            await orch.notify(
                orch.notification(
                    recipient_id=str(anvil_id),
                    message=message,
                    urgency="high",
                )
            )
    except Exception:
        log.debug("email rate limit: #anvil notify failed", exc_info=True)


async def _check_rate_limit(
    requester_id: str,
    to: str,
    cc: str | list[str] | None,
    config: dict,
) -> None:
    from datetime import datetime, timedelta, timezone as dt_timezone

    from db_binding import get_database

    db = get_database()
    cfg = _email_config(config)
    max_per_hour = int(cfg.get("max_sends_per_hour", 10))
    max_domain = int(cfg.get("max_sends_per_domain_per_hour", 3))
    since = (datetime.now(dt_timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")

    req_count = await db.count_email_sends_since(requester_id=requester_id, since_iso=since)
    if req_count >= max_per_hour:
        desc = f"Requester {requester_id} hit {max_per_hour}/hr cap"
        await db_writes.routed("log_activity", 
            event_type="email_rate_limited",
            description=desc,
            person_id=requester_id,
        )
        await _notify_anvil_rate_limit(config, f"🚨 **Email rate limit** — {desc}")
        raise EmailRateLimitError(
            f"Rate limit: {req_count} of {max_per_hour} emails used this hour "
            f"for this user (try again later)."
        )

    domains = {_normalize_email(to).split("@")[-1]} if "@" in _normalize_email(to) else set()
    for addr in _parse_cc(cc):
        if "@" in addr:
            domains.add(addr.split("@")[-1])
    for domain in domains:
        dcount = await db.count_email_sends_since(recipient_domain=domain, since_iso=since)
        if dcount >= max_domain:
            desc = f"Domain {domain} hit {max_domain}/hr cap"
            await db_writes.routed("log_activity", 
                event_type="email_rate_limited",
                description=desc,
                person_id=requester_id,
            )
            await _notify_anvil_rate_limit(config, f"🚨 **Email rate limit** — {desc}")
            raise EmailRateLimitError(
                f"Rate limit: {dcount} of {max_domain} emails used this hour "
                f"to domain '{domain}'."
            )


def _strip_quoted_blocks(body: str) -> str:
    lines = body.splitlines()
    out: list[str] = []
    for line in lines:
        if re.match(r"^>+", line):
            break
        if re.match(r"^-{3,}\s*Original Message\s*-{3,}", line, re.I):
            break
        if re.match(r"^On .+ wrote:\s*$", line):
            break
        if "---------- Forwarded message" in line:
            break
        out.append(line)
    return "\n".join(out).strip()


def _apply_bernie_subject_prefix(subject: str) -> str:
    subj = (subject or "").strip()
    if subj.lower().startswith("[bernie]"):
        return subj
    return f"[Bernie] {subj}" if subj else "[Bernie]"


async def _resolve_reply_recipient(
    to: str,
    *,
    reply_to_gmail_id: str | None,
    thread_id: str | None,
    config: dict,
) -> str:
    """Route replies on forwarded threads to the family forwarder, not external From."""
    from db_binding import get_database

    db = get_database()
    family = family_email_set(config)
    normalized_to = _normalize_email(to)
    if normalized_to in family:
        return to

    lookup_id = reply_to_gmail_id
    if not lookup_id and thread_id:
        sig = await db.get_email_signal_by_thread_id(thread_id)
        if sig and sig.get("gmail_id"):
            lookup_id = sig["gmail_id"]

    if lookup_id:
        sig = await db.get_email_signal_by_gmail_id(lookup_id)
        if sig and sig.get("forwarder_email"):
            fwd = _normalize_email(sig["forwarder_email"])
            if fwd in family:
                return sig["forwarder_email"]

    return to


def _header_map(payload_headers: list[dict] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in payload_headers or []:
        name = (h.get("name") or "").lower()
        if name:
            out[name] = h.get("value") or ""
    return out


def _decode_body_data(data: str | None) -> str:
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "==")
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body_from_payload(payload: dict) -> str:
    mime = payload.get("mimeType") or ""
    body = payload.get("body") or {}
    if mime == "text/plain" and body.get("data"):
        return _decode_body_data(body["data"])
    texts: list[str] = []
    for part in payload.get("parts") or []:
        chunk = _extract_body_from_payload(part)
        if chunk:
            texts.append(chunk)
    return texts[0] if texts else ""


def _parse_gmail_message(msg: dict) -> dict:
    payload = msg.get("payload") or {}
    headers = _header_map(payload.get("headers"))
    internal = int(msg.get("internalDate") or 0)
    from datetime import datetime, timezone as dt_timezone

    received_at = datetime.fromtimestamp(internal / 1000, tz=dt_timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "gmail_id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "received_at": received_at,
        "subject": headers.get("subject", ""),
        "from_header": headers.get("from", ""),
        "delivered_to_header": headers.get("delivered-to") or headers.get("x-original-to") or "",
        "sender_email": _normalize_email(headers.get("from", "")),
        "body_text": _extract_body_from_payload(payload),
        "headers": headers,
    }


async def _run_sync(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


def _list_recent_sync(since_epoch_ms: int | None, limit: int, page_token: str | None) -> dict:
    service = _get_gmail_service()
    q = f"after:{since_epoch_ms // 1000}" if since_epoch_ms else None
    kwargs: dict = {"userId": "me", "maxResults": min(limit, 50)}
    if q:
        kwargs["q"] = q
    if page_token:
        kwargs["pageToken"] = page_token
    return service.users().messages().list(**kwargs).execute()


def _get_message_sync(gmail_id: str) -> dict:
    service = _get_gmail_service()
    fmt = service.users().messages().get(userId="me", id=gmail_id, format="full").execute()
    return _parse_gmail_message(fmt)


def _list_history_sync(start_history_id: str) -> dict:
    service = _get_gmail_service()
    return service.users().history().list(
        userId="me", startHistoryId=start_history_id, historyTypes=["messageAdded"]
    ).execute()


def _profile_history_id_sync() -> str:
    service = _get_gmail_service()
    profile = service.users().getProfile(userId="me").execute()
    return str(profile.get("historyId") or "")


async def list_recent(
    since_epoch_ms: int | None = None,
    limit: int = 20,
    page_token: str | None = None,
) -> dict:
    return await _run_sync(_list_recent_sync, since_epoch_ms, limit, page_token)


async def get_message(gmail_id: str) -> dict:
    return await _run_sync(_get_message_sync, gmail_id)


async def list_history(start_history_id: str) -> dict:
    return await _run_sync(_list_history_sync, start_history_id)


async def get_profile_history_id() -> str:
    return await _run_sync(_profile_history_id_sync)


async def send(
    to: str,
    subject: str,
    body: str,
    *,
    cc: str | list[str] | None = None,
    html: bool = False,
    requester_id: str = "system",
    requester_role: str = "system",
    config: dict | None = None,
    reply_to_gmail_id: str | None = None,
    thread_id: str | None = None,
    policy_checked: bool = False,
) -> str:
    """
    Send email through the policy choke point. Raises EmailPolicyError or
    EmailRateLimitError on failure. Returns Gmail message id.
    """
    if config is None:
        from config import config as _cfg
        config = _cfg

    if not policy_checked:
        action, err = check_send_policy(to, cc, requester_role, config)
        if action == "block":
            raise EmailPolicyError(err or "Email blocked by policy.")
        if action == "approve":
            raise EmailPolicyError(
                "This send requires parent approval in #smithy (kid-initiated)."
            )

    await _check_rate_limit(requester_id, to, cc, config)

    to_resolved = await _resolve_reply_recipient(
        to,
        reply_to_gmail_id=reply_to_gmail_id,
        thread_id=thread_id,
        config=config,
    )
    subject = _apply_bernie_subject_prefix(subject)
    body = _strip_quoted_blocks(body)

    msg_id = await _send_with_retry(
        to_resolved,
        subject,
        body,
        cc,
        html,
        thread_id,
    )

    from db_binding import get_database

    db = get_database()
    from datetime import datetime, timezone as dt_timezone

    sent_at = datetime.now(dt_timezone.utc).isoformat().replace("+00:00", "Z")
    domain = _normalize_email(to_resolved).split("@")[-1] if "@" in _normalize_email(to_resolved) else ""
    await db_writes.routed("record_email_send", 
        requester_id=requester_id,
        recipient=_normalize_email(to_resolved),
        recipient_domain=domain,
        sent_at=sent_at,
    )

    log.info("Email sent to %s — id %s (requester=%s)", to_resolved, msg_id, requester_id)
    return msg_id


def _send_sync(
    to: str,
    subject: str,
    body: str,
    cc=None,
    html: bool = False,
    thread_id: str | None = None,
) -> str:
    service = _get_gmail_service()
    msg = MIMEText(body, "html") if html else MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    if cc:
        msg["cc"] = ", ".join(cc) if isinstance(cc, (list, tuple)) else str(cc)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body_payload: dict = {"raw": raw}
    if thread_id:
        body_payload["threadId"] = thread_id
    result = service.users().messages().send(userId="me", body=body_payload).execute()
    return result["id"]


def _is_retryable_gmail_error(exc: BaseException) -> bool:
    if isinstance(exc, HttpError):
        status = getattr(exc, "status_code", None) or getattr(getattr(exc, "resp", None), "status", None)
        try:
            status = int(status)
        except (TypeError, ValueError):
            return False
        return status in (429, 500, 502, 503, 504)
    return False


async def _send_with_retry(
    to: str,
    subject: str,
    body: str,
    cc=None,
    html: bool = False,
    thread_id: str | None = None,
) -> str:
    delays = (2, 4, 8)
    last_exc: BaseException | None = None
    for attempt, delay in enumerate(delays):
        try:
            return await _run_sync(_send_sync, to, subject, body, cc, html, thread_id)
        except Exception as exc:
            last_exc = exc
            if attempt < len(delays) - 1 and _is_retryable_gmail_error(exc):
                log.warning("Gmail send retry %d after error: %s", attempt + 1, exc)
                await asyncio.sleep(delay)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("Gmail send failed without exception")


# Back-compat alias used by older call sites during migration.
async def send_email(*args, **kwargs) -> str:
    return await send(*args, **kwargs)