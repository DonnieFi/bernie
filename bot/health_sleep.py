"""Health/sleep query detection and authoritative data prefetch.

Wearable numbers must come from live tools, not model memory. When a turn
matches, we fetch Garmin (get_sleep_summary) and Oura (get_oura_sleep) before
the executor runs and inject the payloads into the system prompt.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("bernie")

# Tunable via executor.health_sleep_patterns (empty list → built-in defaults).
DEFAULT_HEALTH_SLEEP_PATTERNS = [
    r"\bhow (did|have|was|well).*\bsleep",
    r"\bsleep (last night|yesterday|tonight|score|quality|data)\b",
    r"\b(last night|yesterday).*\bsleep\b",
    r"\boura\b.*\b(garmin|sleep|score|tracker|ring)\b",
    r"\bgarmin\b.*\b(oura|sleep|score|tracker)\b",
    r"\bcompare\b.*\b(oura|garmin|sleep|tracker|ring)\b",
    r"\b(oura|garmin)\b.*\bcompare\b",
    r"\bsleep score\b",
    r"\breadiness score\b",
    r"\bbody battery\b.*\bsleep\b",
    r"\bsleep\b.*\bbody battery\b",
]

_AUTHORITATIVE_HEADER = """\
--- AUTHORITATIVE HEALTH DATA (mandatory) ---
The JSON below was fetched live from get_sleep_summary (Garmin via Home Assistant) \
and get_oura_sleep (Oura API) seconds ago.

Rules for this turn:
- Reproduce every numeric value in each tool's `core` object exactly as given.
- Do not round, invent, or reuse numbers from earlier conversation turns.
- If a source reports an error or no_data, say so plainly — never guess.
- For Oura vs Garmin comparisons, use ONLY these two payloads side by side.
"""


@dataclass
class HealthPrefetchStatus:
    """Result of prefetch_health_sleep — use `.ok` before trusting model output."""

    block: str | None
    garmin_ok: bool
    oura_ok: bool
    garmin_detail: str = ""
    oura_detail: str = ""

    @property
    def ok(self) -> bool:
        return self.garmin_ok and self.oura_ok

    @property
    def skipped_summary(self) -> str:
        parts: list[str] = []
        if not self.garmin_ok:
            parts.append(f"garmin: {self.garmin_detail or 'failed'}")
        if not self.oura_ok:
            parts.append(f"oura: {self.oura_detail or 'failed'}")
        return "; ".join(parts) if parts else "unknown"


def looks_health_sleep_query(user_message: str, app_config: dict | None = None) -> bool:
    """True when the user message needs live Oura and/or Garmin sleep data."""
    if not user_message or not user_message.strip():
        return False
    text = user_message.lower()
    cfg = (app_config or {}).get("executor", {})
    patterns_cfg = cfg.get("health_sleep_patterns")
    patterns = patterns_cfg if patterns_cfg else DEFAULT_HEALTH_SLEEP_PATTERNS
    return any(re.search(p, text) for p in patterns)


def _source_ok(raw: str, tool_name: str) -> tuple[bool, str]:
    """True when tool output looks like live JSON data, not an error string."""
    if not raw or not raw.strip():
        return False, "empty response"
    stripped = raw.strip()
    if stripped.startswith("No sleep profile"):
        return False, stripped[:160]
    if stripped.startswith("Oura:"):
        return False, stripped[:160]
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return False, "non-json response"
    if tool_name == "get_sleep_summary":
        if not isinstance(payload, dict) or "core" not in payload:
            return False, "missing core"
        return True, "ok"
    if tool_name == "get_oura_sleep":
        if not isinstance(payload, dict):
            return False, "not a dict"
        if payload.get("no_data"):
            return False, f"no_data for {payload.get('date', '?')}"
        return True, "ok"
    return True, "ok"


async def prefetch_health_sleep(
    *,
    config: dict,
    services: Any,
    person_id: str | None,
    group: str,
    channel_id: str | None,
) -> HealthPrefetchStatus:
    """Fetch Garmin + Oura sleep snapshots; return status + optional system block."""
    from executor import ToolContext
    from tool_gateway import get_tool_gateway

    gateway = get_tool_gateway()

    canonical = person_id or "dad"
    ctx = ToolContext(
        config=config,
        person_id=canonical,
        group=group or "family",
        channel_id=channel_id,
        shadow=False,
        executor="prefetch",
        services=services,
        prompt_hash=None,
    )

    garmin_raw = await gateway.execute(
        "get_sleep_summary",
        {"person": canonical, "source": "garmin", "extras": True},
        ctx,
    )
    oura_raw = await gateway.execute("get_oura_sleep", {}, ctx)

    if not isinstance(garmin_raw, str):
        garmin_raw = json.dumps(garmin_raw, default=str)
    if not isinstance(oura_raw, str):
        oura_raw = json.dumps(oura_raw, default=str)

    garmin_ok, garmin_detail = _source_ok(garmin_raw, "get_sleep_summary")
    oura_ok, oura_detail = _source_ok(oura_raw, "get_oura_sleep")

    block = None
    if garmin_ok or oura_ok:
        block = (
            f"{_AUTHORITATIVE_HEADER}\n"
            f"get_sleep_summary (Garmin):\n{garmin_raw}\n\n"
            f"get_oura_sleep (Oura):\n{oura_raw}"
        )

    return HealthPrefetchStatus(
        block=block,
        garmin_ok=garmin_ok,
        oura_ok=oura_ok,
        garmin_detail=garmin_detail,
        oura_detail=oura_detail,
    )


async def record_health_sleep_prefetch(
    status: HealthPrefetchStatus,
    *,
    db_module: Any,
    person_id: str | None,
    channel_id: str | None,
    user_message: str,
) -> None:
    """Write activity_log + structured logs so skipped/failed prefetches are visible."""
    garmin_flag = "ok" if status.garmin_ok else "SKIPPED"
    oura_flag = "ok" if status.oura_ok else "SKIPPED"
    overall = "ok" if status.ok else "INCOMPLETE"

    description = (
        f"health_sleep_prefetch {overall}: "
        f"get_sleep_summary={garmin_flag} get_oura_sleep={oura_flag}"
    )
    meta = json.dumps(
        {
            "garmin_ok": status.garmin_ok,
            "oura_ok": status.oura_ok,
            "garmin_detail": status.garmin_detail,
            "oura_detail": status.oura_detail,
            "query": (user_message or "")[:200],
        },
        default=str,
    )

    if db_module and hasattr(db_module, "log_activity"):
        try:
            await db_module.log_activity(
                event_type="health_sleep_prefetch",
                description=description,
                meta=meta,
                channel=str(channel_id) if channel_id else None,
                person_id=person_id,
            )
        except Exception:
            log.warning("health_sleep: activity_log write failed", exc_info=True)

    if status.ok:
        log.info(
            "health_sleep_prefetch ok: garmin+oura for person=%s channel=%s",
            person_id or "dad",
            channel_id or "?",
        )
    else:
        log.warning(
            "health_sleep_prefetch SKIPPED or incomplete: %s (channel=%s)",
            status.skipped_summary,
            channel_id or "?",
        )


def log_health_sleep_turn_tools(
    *,
    prefetch_ok: bool,
    model_tools_called: set[str],
    channel_id: str | None,
) -> None:
    """After the model turn: note if the executor called sleep tools (redundant or fallback)."""
    sleep_tools = model_tools_called & {"get_sleep_summary", "get_oura_sleep"}
    if prefetch_ok:
        if sleep_tools:
            log.info(
                "health_sleep: model also called %s (prefetch already injected; channel=%s)",
                sorted(sleep_tools),
                channel_id or "?",
            )
        return
    if not sleep_tools:
        log.warning(
            "health_sleep: SKIPPED — no successful prefetch and model did not call "
            "get_sleep_summary/get_oura_sleep (channel=%s)",
            channel_id or "?",
        )
