import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from enum import Enum

from executor import ToolContext, ServiceRefs
from tools import effective_tier
import db_writes

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _db_module(services: ServiceRefs | None = None):
    """Resolve database module from ServiceRefs or the process default."""
    if services is not None and services.db is not None:
        return services.db
    from db_binding import get_database
    return get_database()


class HitlDecision(Enum):
    PROCEED = "proceed"   # run dispatch
    HELD = "held"         # tier 3 hold — return synthetic string, no dispatch


DEFAULT_HITL_TIMEOUT_SEC = 300
CTX_JSON_ALLOWLIST = frozenset({
    "person_id", "group", "channel_id", "shadow", "executor",
    "prompt_hash", "task_id", "mode",
})


def serialize_ctx(ctx: ToolContext) -> str:
    """Serialize the ToolContext to JSON using only allowlisted keys."""
    ctx_dict = {}
    for key in CTX_JSON_ALLOWLIST:
        if hasattr(ctx, key):
            ctx_dict[key] = getattr(ctx, key)
    return json.dumps(ctx_dict)


def deserialize_ctx(ctx_json: str) -> dict:
    """Parse JSON and raise ValueError if any key is not in CTX_JSON_ALLOWLIST."""
    data = json.loads(ctx_json)
    if not isinstance(data, dict):
        raise ValueError("Serialized context must be a JSON object")
    for key in data.keys():
        if key not in CTX_JSON_ALLOWLIST:
            raise ValueError(f"Key {key!r} not in context JSON allowlist")
    return data


def rebuild_tool_context(ctx_blob: dict, *, services: ServiceRefs) -> ToolContext:
    """Rebuild ToolContext from ctx_blob and live config/services."""
    from config import load_config
    app_config = load_config()
    return ToolContext(
        config=app_config,
        person_id=ctx_blob.get("person_id"),
        group=ctx_blob.get("group", "family"),
        channel_id=ctx_blob.get("channel_id"),
        shadow=ctx_blob.get("shadow", False),
        executor=ctx_blob.get("executor", "native"),
        services=services,
        prompt_hash=ctx_blob.get("prompt_hash"),
        task_id=ctx_blob.get("task_id"),
        mode=ctx_blob.get("mode"),
    )


async def _notify_admins_for_hold(pending_id: int, db) -> None:
    """Inline or cross-container DM notify with short retries."""
    import os

    role = os.environ.get("ROLE")

    async def _inline() -> None:
        from hitl.hitl_discord import get_inline_notifier
        notifier = get_inline_notifier()
        if not notifier:
            raise RuntimeError("inline HITL notifier not registered")
        await notifier(pending_id)

    async def _cross() -> None:
        from cross_container import notify_hitl_pending
        await notify_hitl_pending(pending_id)

    notify = _inline if role in ("discord", "monolith") or not role else _cross
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            await notify()
            return
        except Exception as exc:
            last_err = exc
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
    log.error(
        "Failed to notify admins for pending HITL request #%d after retries: %s",
        pending_id,
        last_err,
        exc_info=last_err,
    )
    await db_writes.routed("log_activity", 
        event_type="hitl_notify_failed",
        description=f"HITL notify failed for request #{pending_id}",
        meta=json.dumps({"pending_id": pending_id, "error": str(last_err)}),
    )


async def check_tier(gateway, name: str, args: dict, ctx: ToolContext, entry: dict) -> tuple[HitlDecision, str | None]:
    """Check tool tier; Tier 1 and 2 proceed, Tier 3 is HELD."""
    if getattr(ctx, "hitl_approved", False):
        return HitlDecision.PROCEED, None

    tier = effective_tier(entry)
    if tier in (1, 2):
        return HitlDecision.PROCEED, None

    # Tier 3 holds tool execution
    db = _db_module(ctx.services)

    args_json = json.dumps(args)
    ctx_json = serialize_ctx(ctx)

    now = datetime.now(timezone.utc)
    requested_at = _utc_now_iso()
    expires_at = (now + timedelta(seconds=DEFAULT_HITL_TIMEOUT_SEC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    reasoning = args.get("reasoning") if isinstance(args, dict) else None

    pending_id = await db_writes.routed("create_pending_hitl", 
        tool_name=name,
        args_json=args_json,
        ctx_json=ctx_json,
        reasoning=reasoning,
        expires_at=expires_at,
        requested_at=requested_at,
    )

    await db_writes.routed("log_activity", 
        event_type="hitl_held",
        description=f"Action '{name}' held for admin approval (request #{pending_id})",
        person_id=ctx.person_id,
        meta=json.dumps({"pending_id": pending_id, "tool_name": name}),
    )

    await _notify_admins_for_hold(pending_id, db)

    hold_msg = f"Action '{name}' requires admin approval (request #{pending_id}). I'll proceed once approved."
    return HitlDecision.HELD, hold_msg


async def resume_pending(
    pending_id: int,
    gateway,
    *,
    services: ServiceRefs,
    decided_by: str,
) -> str:
    """Resume a pending tool request by rebuilding context and executing via gateway."""
    db = _db_module(services)

    row = await db.get_pending_hitl(pending_id)
    if not row:
        return f"Error: Request #{pending_id} not found."

    resolved = await db_writes.routed("resolve_pending_hitl", pending_id, "approved", decided_by)
    if not resolved:
        await db_writes.routed("expire_stale_pending_hitl", _utc_now_iso())
        return "Request expired or already decided."

    ctx_blob = deserialize_ctx(row["ctx_json"])
    ctx = rebuild_tool_context(ctx_blob, services=services)
    ctx.hitl_approved = True
    ctx.hitl_pending_id = pending_id

    args = json.loads(row["args_json"])

    await db_writes.routed("log_activity", 
        event_type="hitl_approved",
        description=f"Action '{row['tool_name']}' approved (request #{pending_id})",
        person_id=ctx.person_id,
        meta=json.dumps({"pending_id": pending_id, "tool_name": row["tool_name"], "decided_by": decided_by}),
    )

    result = await gateway.execute(row["tool_name"], args, ctx)
    return str(result)


async def deny_pending(
    pending_id: int,
    *,
    services: ServiceRefs,
    decided_by: str,
) -> bool:
    """Atomically deny a pending hold. Returns False if already decided."""
    db = _db_module(services)
    resolved = await db_writes.routed("resolve_pending_hitl", pending_id, "denied", decided_by)
    if not resolved:
        return False

    await db_writes.routed("log_activity", 
        event_type="hitl_denied",
        description=f"Action held in request #{pending_id} denied",
        person_id=decided_by,
        meta=json.dumps({"pending_id": pending_id, "decided_by": decided_by}),
    )
    return True


async def run_hitl_expiry_sweep(services: ServiceRefs | None = None) -> int:
    """Expire stale pending requests and log one activity row per expired id."""
    db = _db_module(services)
    now_iso = _utc_now_iso()

    pending_rows = await db.list_pending_hitl(status="pending")
    candidate_ids = [row["id"] for row in pending_rows if row["expires_at"] <= now_iso]
    if not candidate_ids:
        return 0

    count = await db_writes.routed("expire_stale_pending_hitl", now_iso)
    if count:
        log.info("Expired %d stale pending HITL request(s)", count)
    for pid in candidate_ids:
        row = await db.get_pending_hitl(pid)
        if row and row["status"] == "expired":
            await db_writes.routed("log_activity", 
                event_type="hitl_expired",
                description=f"Pending HITL request #{pid} expired",
                meta=json.dumps({"pending_id": pid, "decided_by": row.get("decided_by")}),
            )
    return count


async def run_hitl_purge(*, older_than_days: int = 7, services: ServiceRefs | None = None) -> int:
    """Delete terminal pending_hitl rows older than ``older_than_days``."""
    db = _db_module(services)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=older_than_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return await db_writes.routed("purge_terminal_pending_hitl", cutoff)
