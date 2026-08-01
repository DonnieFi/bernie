"""ToolGateway — single dispatcher for every tool call.

All executors (NativeToolExecutor, SmolExecutor, future variants) route
their tool calls through `ToolGateway.execute()`. The gateway owns:

1. **Unknown-tool errors**
2. **RBAC (Final Guard)** — group→role_required check is the single chokepoint.
3. **JSON-Schema validation** of args
4. **Shadow-write blocking** — if `ctx.shadow` is true and the tool is
   `is_write=True`, the gateway returns a synthetic message instead of
   dispatching. Handlers also do this guard as defense-in-depth.
5. **Phase 29 tier hook** — HITL tier gate via `hitl.hitl_service.check_tier`.
6. **Dispatch + structured Langfuse span + activity_log entry**.

RBAC hierarchy:
- `ROLE_ALL` ("all") — any caller (family, kids, parents, admin).
- `ROLE_PARENTS` ("parents") — system, admin, parents.
- `ROLE_BERNIE` ("bernie") — system, admin (workers/bernie context).
- `ROLE_ADMIN` ("admin") — system, admin.
- `ROLE_SYSTEM` ("system") — full access.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

import jsonschema
import db_writes

log = logging.getLogger(__name__)


class ToolValidationError(ValueError):
    """Raised when a tool call's args fail JSON-Schema validation.

    Carries the user-facing message that should be fed back to the model
    so it can retry. Executors catch this to drive per-turn retry/escalation
    rather than parsing prose.
    """

    def __init__(self, tool_name: str, message: str) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.message = message


def _coerce_tool_args(args: dict, schema: dict) -> dict:
    """Map common LLM arg aliases onto schema property names before validation.

    Non-Anthropic models (e.g. DeepSeek via LiteLLM) often emit ``start`` /
    ``end`` instead of ``start_date`` / ``end_date``. Only fills missing
    canonical keys — explicit canonical values win.
    """
    props = schema.get("properties") or {}
    if not props:
        return args
    out = dict(args)
    for prop in props:
        if prop in out:
            continue
        if prop.endswith("_date"):
            short = prop[: -len("_date")]
            if short in out:
                out[prop] = out.pop(short)
        elif prop.endswith("_name"):
            short = prop[: -len("_name")]
            if short in out:
                out[prop] = out.pop(short)
    return out


def _result_cap_for(tool_name: str) -> int:
    """Resolve per-call truncation cap for `tool_name`.

    Reads `tool_gateway.max_result_chars` (default 6000) and per-tool overrides
    from `tool_gateway.per_tool_max_chars` in config.json. A value of 0 disables
    truncation for that tool. Reads config lazily so /reload takes effect
    without a restart.
    """
    try:
        from config import config as _cfg
    except Exception:
        return 6000
    gw_cfg = _cfg.get("tool_gateway", {}) or {}
    default = int(gw_cfg.get("max_result_chars", 6000) or 0)
    per_tool = gw_cfg.get("per_tool_max_chars", {}) or {}
    raw = per_tool.get(tool_name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _truncate_tool_result(result: str, cap: int) -> str:
    """Truncate oversized tool returns without breaking JSON snapshot payloads.

    Snapshot tools (flights, vehicle, sleep, …) return ``{summary, core, …}``.
    Blind string slicing produces invalid JSON for the model; prefer dropping
    bulky ``raw``/``extras`` first, then slim to core+summary.
    """
    if cap <= 0 or not isinstance(result, str) or len(result) <= cap:
        return result
    stripped = result.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            import json

            data = json.loads(result)
            if isinstance(data, dict):
                data.pop("raw", None)
                if isinstance(data.get("extras"), dict):
                    data["extras"] = {"_truncated": True}
                out = json.dumps(data, indent=2)
                if len(out) <= cap:
                    return out
                summary = data.get("summary")
                if isinstance(summary, str) and len(summary) > max(200, cap // 4):
                    data["summary"] = summary[: max(200, cap // 4)] + "…"
                slim = {
                    "summary": data.get("summary", ""),
                    "core": data.get("core", data),
                    "truncated": True,
                }
                out = json.dumps(slim, indent=2)
                if len(out) <= cap:
                    return out
                return json.dumps(
                    {
                        "summary": (str(slim.get("summary") or ""))[:200],
                        "truncated": True,
                        "error": "result too large",
                    }
                )
            # list / non-dict JSON — re-dump truncated representation
            out = json.dumps(data)
            if len(out) <= cap:
                return out
        except Exception:
            pass
    return result[:cap] + "\n[…truncated — call with narrower filters for more detail]"


def _role_allowed(caller_group: str | None, role_required: str) -> bool:
    """RBAC filter: does caller_group satisfy role_required?"""
    if caller_group is None:
        return False
    if caller_group == "system":
        return True
    if role_required == "all":
        return True
    if role_required == "parents" and caller_group in ("system", "admin", "parents"):
        return True
    if role_required == "admin" and caller_group in ("system", "admin"):
        return True
    if role_required == "bernie" and caller_group in ("system", "admin"):
        return True
    return False


class ToolGateway:
    def __init__(self, registry: dict[str, dict]) -> None:
        self._registry = registry
        # Hold strong refs to fire-and-forget span/activity tasks so the GC
        # doesn't reap them mid-flight. Discarded on completion.
        self._bg_tasks: set[asyncio.Task] = set()

    def get_tool_schemas(
        self,
        group: str | None,
        cal_available: bool = True,
        domains: list[str] | None = None,
    ) -> list[dict]:
        """Return Anthropic-format tool dicts accessible to this caller group.

        IMPORTANT: strips internal metadata (role_required, is_write, fn) —
        Anthropic API rejects unknown keys.

        Sorted alphabetically by name for deterministic KV-cache prefix hashing.
        """
        out: list[dict] = []
        for entry in self._registry.values():
            if not _role_allowed(group, entry.get("role_required", "all")):
                continue
            if not cal_available and entry.get("domain") == "calendar":
                continue
            if domains is not None and entry.get("domain") not in domains:
                continue
            out.append({
                "name": entry["name"],
                "description": entry["description"],
                "input_schema": entry["input_schema"],
            })
        out.sort(key=lambda t: t["name"])
        return out

    async def execute(self, name: str, args: dict, ctx):
        """Single entry point for all tool calls from all executors.

        Returns whatever the handler returns — usually a `str`, but may be a
        `list` of Anthropic content blocks (e.g. image responses). Executors
        should pass the value through unchanged and not assume str.

        Raises `ToolValidationError` when args fail JSON-Schema validation
        so executors can drive retry/escalation without parsing return prose.
        """
        # family-bot-1ov.4: domains load at process startup (main/bot) or once via
        # get_tool_gateway(). Do not re-import every execute — only cold-start if
        # this gateway's registry dict is still empty (tests / early callers).
        if not self._registry:
            from tools import load_all_domains

            load_all_domains()

        # ── Step 1: Unknown tool ────────────────────────────────────────────
        entry = self._registry.get(name)
        if entry is None:
            log.warning("ToolGateway: unknown tool %r", name)
            return f"Unknown tool: {name}"

        # ── Step 2: RBAC Final Guard ────────────────────────────────────────
        required = entry.get("role_required", "all")
        if not _role_allowed(ctx.group, required):
            log.warning(
                "RBAC denied: group=%s tool=%s requires=%s actor=%s",
                ctx.group, name, required, ctx.person_id,
            )
            if ctx.group == "kids":
                return (
                    f"I'm sorry, but using the '{name}' tool is restricted to "
                    "parents or admins. You'll need to ask a parent to do that for you."
                )
            return (
                f"Access Denied: The '{name}' tool requires the '{required}' role. "
                f"Your current role is '{ctx.group}'."
            )

        # ── Step 3: JSON-Schema validation ──────────────────────────────────
        # Strip None values — LLMs routinely emit `null` for optional fields
        # they don't want to use, and jsonschema rejects them as type errors.
        if isinstance(args, dict):
            args = {k: v for k, v in args.items() if v is not None}
        schema = entry.get("input_schema")
        if schema and isinstance(args, dict):
            args = _coerce_tool_args(args, schema)
        if schema:
            try:
                jsonschema.validate(instance=args, schema=schema)
            except jsonschema.ValidationError as exc:
                log.warning("ToolGateway: schema validation failed for %s: %s", name, exc.message)
                props = list((schema.get("properties") or {}).keys())
                required = schema.get("required") or []
                raise ToolValidationError(
                    tool_name=name,
                    message=(
                        f"Invalid arguments for '{name}': {exc.message}. "
                        f"Expected parameter names: {props}. Required: {required}. "
                        "Please try again with corrected arguments."
                    ),
                )

        # ── Step 4: Shadow-write guard (defense in depth) ───────────────────
        if ctx.shadow and entry.get("is_write"):
            return f"[shadow: would have called {name}({args})]"

        # ── Step 5: Phase 29 tier hook ──────────────────────────────────────
        from hitl.hitl_service import check_tier, HitlDecision

        decision, hold_msg = await check_tier(self, name, args, ctx, entry)
        if decision == HitlDecision.HELD:
            return hold_msg

        # ── Step 6: Dispatch ────────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            result = await entry["fn"](args, ctx)
        except Exception as exc:
            log.exception("ToolGateway: handler %r raised", name)
            result = f"Error executing '{name}': {exc}"
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        from tools import effective_tier
        if effective_tier(entry) == 2 and not ctx.shadow:
            import json
            db = ctx.services.db if ctx.services and ctx.services.db else None
            if db is None:
                from db_binding import get_database
                db = get_database()
            self._spawn_bg(db.log_activity(
                event_type="hitl_tier2_stub",
                description=f"Tier 2 tool '{name}' dispatched",
                person_id=ctx.person_id,
                meta=json.dumps({"tool_name": name}),
            ))
            from hitl.hitl_discord import post_tier2_anvil_audit
            audit_args = args if isinstance(args, dict) else {}
            self._spawn_bg(post_tier2_anvil_audit(
                tool_name=name,
                args=audit_args,
                ctx=ctx,
                elapsed_ms=elapsed_ms,
            ))

        # ── Step 7: Truncate oversized results ─────────────────────────────
        # Prevents single tool returns from ballooning the next turn's input
        # (the 62k-token spike pattern identified in the prompt-bloat audit).
        # Cap is read from config so tools that legitimately need more room
        # (web_search, get_events_range, get_home_state query=…) can be
        # raised without a code change. Set the per-tool value to 0 to disable.
        # JSON snapshot payloads are slimmed without producing invalid JSON.
        cap = _result_cap_for(name)
        if isinstance(result, str):
            result = _truncate_tool_result(result, cap)

        # ── Step 8: Langfuse span + activity_log (best-effort, fire-and-forget)
        self._spawn_bg(self._emit_span(name, args, result, elapsed_ms, ctx))
        self._spawn_bg(self._emit_activity(name, ctx))

        return result

    def _spawn_bg(self, coro) -> None:
        """Schedule a background coroutine and retain the Task ref."""
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            # No running loop (e.g. unit test calling .execute() via asyncio.run
            # after the loop has closed). Drop the coroutine cleanly.
            coro.close()
            return
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _emit_span(
        self, name: str, args: dict, result: Any, elapsed_ms: int, ctx
    ) -> None:
        try:
            from langfuse_service import lf_tool_span
            await lf_tool_span(
                tool_name=name,
                args=args,
                result=result,
                elapsed_ms=elapsed_ms,
                executor=ctx.executor,
                shadow=ctx.shadow,
                person_id=ctx.person_id,
            )
        except Exception:
            pass  # Langfuse is non-fatal

    async def _emit_activity(self, name: str, ctx) -> None:
        try:
            db = ctx.services.db if ctx.services and ctx.services.db else None
            if db is None:
                return
            # Use an explicit `shadow_leg=primary|harness` key — the prior
            # `shadow=True/False` form relied on Python bool stringification
            # and was brittle to match against in SQL `LIKE` queries.
            shadow_leg = "harness" if ctx.shadow else "primary"
            meta = f"group={ctx.group} shadow_leg={shadow_leg}"
            if getattr(ctx, "prompt_hash", None):
                meta += f" prompt_hash={ctx.prompt_hash}"
            await db_writes.routed("log_activity", 
                event_type="tool_call",
                description=f"Tool <b>{name}</b> called via {ctx.executor}",
                meta=meta,
                person_id=ctx.person_id,
            )
        except Exception:
            pass


_gateway: ToolGateway | None = None
_gateway_lock = threading.Lock()


def get_tool_gateway() -> ToolGateway:
    """Process-wide ToolGateway (p27 — avoid reconstructing per chat leg)."""
    global _gateway
    if _gateway is not None:
        return _gateway
    with _gateway_lock:
        if _gateway is None:
            from tools import get_registry, load_all_domains

            load_all_domains()
            _gateway = ToolGateway(registry=get_registry())
    return _gateway


def reset_tool_gateway_for_tests() -> None:
    """Clear the process singleton (unittest isolation)."""
    global _gateway
    _gateway = None
