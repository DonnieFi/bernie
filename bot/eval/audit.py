import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# Intentional cross-layer import: audit reuses the shared live-data intent detector.
# Moved to llm.intent in Phase 4.4 Session 0 so eval does not import claude_service
# at module load (and to share the logic with the new routing package).
from llm.intent import looks_live_data

log = logging.getLogger(__name__)

_TOOL_NAME_RE = re.compile(r"Tool <b>(\w+)</b>")
_NUMERIC_CLAIM_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|km\b)|\blocked\b|\bunlocked\b",
    re.I,
)
_AUDIT_WINDOW_MINUTES = 2


def _parse_audit_ts(ts: str | None) -> datetime:
    if not ts:
        return datetime.min.replace(tzinfo=timezone.utc)
    normalized = ts.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_tool_name(description: str) -> str | None:
    m = _TOOL_NAME_RE.search(description or "")
    return m.group(1) if m else None


def _response_has_numeric_claim(text: str) -> bool:
    return bool(_NUMERIC_CLAIM_RE.search(text or ""))


_grounding_tools_cache: frozenset[str] | None = None


def _grounding_tool_names() -> frozenset[str]:
    """Read tools in the registry count as grounding (not a fixed snapshot subset).

    Modes filter which tools Bernie *may call* per turn (domains.allow/deny →
    ToolGateway.get_tool_schemas). This audit only checks whether *any* read
    tool ran near the reply — write tools do not ground numeric live-data claims.

    Cached for process lifetime — registry is stable after startup ``load_all_domains``.
    """
    global _grounding_tools_cache
    if _grounding_tools_cache is not None:
        return _grounding_tools_cache

    from tools import get_registry, load_all_domains

    reg = get_registry()
    if not reg:
        load_all_domains()  # idempotent; startup normally warms the registry
        reg = get_registry()
    _grounding_tools_cache = frozenset(
        name for name, entry in reg.items() if not entry.get("is_write", False)
    )
    return _grounding_tools_cache


def _clear_grounding_tools_cache() -> None:
    """Test helper — reset cached read-tool set."""
    global _grounding_tools_cache
    _grounding_tools_cache = None


def _pair_user_assistant_turns(rows: list[dict]) -> list[tuple[dict, dict]]:
    """Pair each user message with the next assistant reply in the same channel."""
    by_channel: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_channel[str(row.get("channel_id", ""))].append(row)

    pairs: list[tuple[dict, dict]] = []
    for channel_rows in by_channel.values():
        i = 0
        while i < len(channel_rows):
            if channel_rows[i].get("role") != "user":
                i += 1
                continue
            user_row = channel_rows[i]
            j = i + 1
            while j < len(channel_rows) and channel_rows[j].get("role") != "assistant":
                j += 1
            if j < len(channel_rows):
                pairs.append((user_row, channel_rows[j]))
                i = j + 1
            else:
                i += 1
    return pairs


def _tool_call_near_turn(
    tool_calls: list[dict],
    window_start: datetime,
    window_end: datetime,
    *,
    grounding_tools: frozenset[str],
) -> list[str]:
    seen: list[str] = []
    for tc in tool_calls:
        ts = _parse_audit_ts(tc.get("logged_at"))
        if ts < window_start or ts > window_end:
            continue
        name = _parse_tool_name(tc.get("description", ""))
        if name and name in grounding_tools and name not in seen:
            seen.append(name)
    return seen


async def _fetch_conversation_since(db_module, since_iso: str) -> list[dict]:
    return await db_module.fetch_conversation_rows_since(since_iso)


async def _fetch_tool_calls_since(db_module, since_iso: str) -> list[dict]:
    return await db_module.fetch_tool_calls_since(since_iso)


async def audit_ungrounded_live_data(db_module, since_hours: int = 24) -> list[dict]:
    """Flag live-data turns where the assistant claims numbers without a nearby tool call."""
    try:
        from config import config as app_config
    except Exception:
        app_config = {}

    since_dt = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    since_iso = since_dt.isoformat()

    conversation = await _fetch_conversation_since(db_module, since_iso)
    tool_calls = await _fetch_tool_calls_since(db_module, since_iso)
    grounding_tools = _grounding_tool_names()

    flags: list[dict] = []
    for user_row, assistant_row in _pair_user_assistant_turns(conversation):
        user_text = user_row.get("content") or ""
        if not looks_live_data(user_text, app_config or {}):
            continue

        assistant_text = assistant_row.get("content") or ""
        if not _response_has_numeric_claim(assistant_text):
            continue

        user_ts = _parse_audit_ts(user_row.get("created_at"))
        assistant_ts = _parse_audit_ts(assistant_row.get("created_at"))
        window_end = assistant_ts + timedelta(minutes=_AUDIT_WINDOW_MINUTES)
        nearby_tools = _tool_call_near_turn(
            tool_calls, user_ts, window_end, grounding_tools=grounding_tools
        )
        if nearby_tools:
            continue

        flags.append({
            "created_at": user_row.get("created_at", ""),
            "channel_id": user_row.get("channel_id"),
            "user_message": user_text[:200],
            "response_snippet": assistant_text[:200],
            "reason": "numeric_claim_without_tool_call",
        })

    return flags


def format_ungrounded_audit_section(flags: list[dict]) -> str:
    """Discord digest section for ungrounded live-data turns."""
    if not flags:
        return ""
    lines = [
        "",
        f"**Ungrounded live data** — {len(flags)} turn(s)",
        "_Assistant gave numeric/lock claims with no read tool call within 2 min_",
    ]
    for row in flags[:8]:
        ts = (row.get("created_at") or "")[:16]
        user = row.get("user_message", "")[:70]
        snippet = row.get("response_snippet", "")[:90]
        lines.append(f"• `{ts}` _{user}_")
        if snippet:
            lines.append(f"  ↳ {snippet}")
    if len(flags) > 8:
        lines.append(f"_…and {len(flags) - 8} more_")
    return "\n".join(lines)
