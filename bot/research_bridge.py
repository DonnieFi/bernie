"""Bridge between board `research` unified_tasks and the cognitive_tasks ResearchWorker."""
import logging
import os
import re
import html as _htmlmod
from db_binding import get_database
import db_writes

log = logging.getLogger(__name__)


def _data_dir() -> str:
    """Directory holding the DB + persistent artifacts (mounted ./data on the host)."""
    return os.path.dirname(get_database().DB_PATH) or "."


def _slug(s: str, n: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return (s[:n].strip("-") or "research")


def _md_to_html(md: str) -> str:
    """Minimal, safe markdown → HTML (headings, lists, bold). Everything escaped first."""
    out = []
    in_list = False
    for line in (md or "").split("\n"):
        esc = _htmlmod.escape(line)
        is_li = line.lstrip().startswith(("- ", "* "))
        if not is_li and in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("### "): out.append(f"<h3>{esc[4:]}</h3>")
        elif line.startswith("## "): out.append(f"<h2>{esc[3:]}</h2>")
        elif line.startswith("# "): out.append(f"<h1>{esc[2:]}</h1>")
        elif is_li:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{esc.lstrip()[2:]}</li>")
        elif not line.strip(): out.append("")
        else: out.append(f"<p>{esc}</p>")
    if in_list:
        out.append("</ul>")
    body = "\n".join(out)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)


def _archive_html(unified_task_id: int, title: str, content: str) -> str:
    """Write the research result to a local HTML file for future reference; return the path."""
    from datetime import datetime, timezone
    d = os.path.join(_data_dir(), "research")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"research-{unified_task_id}-{_slug(title)}.html")
    ts = datetime.now(timezone.utc).isoformat()
    doc = (
        '<!doctype html><meta charset="utf-8"><title>' + _htmlmod.escape(title) + "</title>"
        "<style>body{font:16px/1.6 system-ui,-apple-system,sans-serif;max-width:820px;"
        "margin:40px auto;padding:0 20px;color:#1a1a1a}h1,h2,h3{line-height:1.25}"
        "li{margin:.2em 0}.meta{color:#888;font:12px/1.5 ui-monospace,monospace;margin-bottom:18px}</style>"
        f'<div class="meta">Bernie research · task #{unified_task_id} · {ts}</div>\n'
        + _md_to_html(content)
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path


def _assigner_email(canonical_id: str) -> str | None:
    """Resolve a family member's email from config by canonical id (e.g. 'dad')."""
    if not canonical_id or canonical_id.startswith("agent:"):
        return None
    from config import config as cfg
    for v in (cfg.get("family_members") or {}).values():
        if v.get("canonical_id") == canonical_id:
            return v.get("email")
    return None


async def _deliver_research(
    unified_task_id: int,
    plan,
    *,
    task_store=None,
    container=None,
) -> None:
    """Email the assigner the reviewed result and archive HTML. Best-effort."""
    import asyncio

    from delivery_gateway import send_email_via_gateway

    store = task_store or get_database()
    task = await store.get_task(unified_task_id)
    title = (task or {}).get("title", "research")
    content = plan.body_text
    html_path = emailed_to = None
    try:
        html_path = _archive_html(unified_task_id, title, content)
        log.info("research bridge: archived HTML for task #%s → %s", unified_task_id, html_path)
    except Exception:
        log.warning("research bridge: HTML archive failed for #%s", unified_task_id, exc_info=True)
    try:
        from config import config as _cfg

        from email_service import resolve_family_cc_email

        cc_addr = resolve_family_cc_email(_cfg, "research_cc_email")
        to_addr = _assigner_email((task or {}).get("assigned_by", "")) or cc_addr
        cc = cc_addr if cc_addr and cc_addr != to_addr else None
        subject = f"{plan.prefix}Research: {title[:120]}"
        body = f"{content}\n\n— Bernie · task #{unified_task_id}"
        await asyncio.wait_for(
            send_email_via_gateway(
                to=to_addr,
                subject=subject,
                body=body,
                cc=cc,
                config=_cfg,
                container=container,
            ),
            timeout=10,
        )
        emailed_to = to_addr
    except Exception:
        log.warning("research bridge: email delivery failed for #%s", unified_task_id, exc_info=True)
    await store.add_task_event(
        unified_task_id,
        "delivered",
        "agent:research-worker",
        {"email": emailed_to, "html": html_path, "route": plan.route},
    )


async def enqueue_for_unified(
    unified_task_id: int,
    topic: str,
    actor_id: str = "",
    task_store=None,
    task_service=None,
) -> None:
    """Enqueue a ResearchWorker cognitive task for a unified board research task.

    Prefer ``task_service`` (``UnifiedTaskService.enqueue_research_run``); legacy
    callers may pass ``task_store`` only and get a short-lived facade instance.
    """
    if task_service is not None:
        await task_service.enqueue_research_run(
            unified_task_id, topic, actor_id=actor_id,
        )
        return
    from services.unified_task_service import UnifiedTaskService

    try:
        from config import config as app_config
    except ImportError:
        app_config = {}
    store = task_store or get_database()
    svc = UnifiedTaskService(
        task_store=store,
        config=app_config,
        notification_router=None,
    )
    await svc.enqueue_research_run(unified_task_id, topic, actor_id=actor_id)


async def finalize_unified_from_research(unified_task_id: int, *, ok: bool, summary: str,
                                         task_store=None,
                                         run_id: str, error: str | None = None,
                                         logs: str | None = None, metrics: dict | None = None,
                                         deliver: bool = False,
                                         notification_router=None,
                                         container=None) -> None:
    db = get_database()
    store = task_store or db
    await store.start_execution(unified_task_id, run_id)   # INSERT OR IGNORE — safe whether or not forward path made the row
    if ok:
        from config import config as _cfg
        from research_executive_delivery import prepare_research_for_delivery

        task = await store.get_task(unified_task_id)
        title = (task or {}).get("title", "research")
        assigner = (task or {}).get("assigned_by", "")
        plan = await prepare_research_for_delivery(
            summary or "",
            title,
            config=_cfg,
            container=container,
            db=store,
            requester_id=assigner,
            source="research_bridge",
        )
        note = plan.body_text
        await store.finish_execution(run_id, status="completed", logs=note[:4000], metrics=metrics or {})
        await store.complete_task(unified_task_id, completion_note=note)
        await store.add_task_event(
            unified_task_id,
            "completed",
            "agent:research-worker",
            {"via": "bridge", "route": plan.route},
        )
        await store.promote_ready_tasks()
        if deliver and plan.should_deliver:
            await _deliver_research(
                unified_task_id,
                plan,
                task_store=task_store,
                container=container,
            )
    else:
        await store.finish_execution(run_id, status="crashed", logs=logs or error or "failed", metrics=metrics or {})
        await store.block_task(unified_task_id, error or "research failed")
        await store.add_task_event(unified_task_id, "blocked", "agent:research-worker", {"error": error})
        # Ping the assigner (or enqueue for #anvil) when the research worker blocks a task
        try:
            from notify_targets import blocked_ping_recipient
            task = await store.get_task(unified_task_id)
            recipient = blocked_ping_recipient(task or {})
            msg = f"⚠ Task #{unified_task_id} blocked: {error or 'research failed'}"
            _notified = False
            if recipient:
                try:
                    from task_access import person_to_discord_id
                    did = person_to_discord_id(recipient)
                    if did and notification_router:
                        await notification_router.notify(notification_router.notification(recipient_id=str(did), message=msg))
                        _notified = True
                except Exception:
                    log.warning("research bridge: direct blocked-ping failed for #%s, falling back to #anvil",
                                unified_task_id, exc_info=True)
            if not _notified:
                from config import config as _cfg
                anvil_id = _cfg.get("anvil_channel_id") or ""
                target_id = str(anvil_id) if anvil_id else "admin"
                if notification_router and anvil_id:
                    await notification_router.notify(notification_router.notification(recipient_id=str(anvil_id), message=msg))
                    _notified = True
                if not _notified:
                    if store is db:
                        await db_writes.routed("add_pending_notification", recipient_id=target_id, message=msg)
                    elif hasattr(store, "add_pending_notification"):
                        await store.add_pending_notification(recipient_id=target_id, message=msg)
        except Exception:
            log.warning("research bridge: blocked-ping wholly failed for #%s", unified_task_id, exc_info=True)
