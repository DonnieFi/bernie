"""Front-end intent router — tool surface narrowing without an LLM call.

Layers on mode ``domains.allow``: chit-chat → no tools; clear signals → core + matched
domains; ambiguous → full mode allowlist.
"""

from __future__ import annotations

import re
from typing import Any

from .context_legs import looks_schedule_intent, calendar_prefetch_mode, _channel_is

# Always-on family surface when intent is clear but domain is narrow.
_CORE_DOMAINS = frozenset({
    "calendar", "weather", "memory", "notify", "tasks", "search",
})


def _core_domains_for(config: dict[str, Any]) -> frozenset[str]:
    """Core surface bundled with a matched domain; calendar omitted when lazy."""
    router_cfg = (config.get("context") or {}).get("intent_router") or {}
    override = router_cfg.get("core_domains")
    if isinstance(override, list) and override:
        core = set(override)
    else:
        core = set(_CORE_DOMAINS)
    if calendar_prefetch_mode(config) in ("lazy", "never", "tools_only"):
        core.discard("calendar")
    return frozenset(core)

_DOMAIN_PATTERNS: dict[str, list[str]] = {
    "transit": [
        r"\bbus\b", r"\btransit\b", r"\broute\s+\d+", r"\b/bus\b",
        r"\bhalifax transit\b",
    ],
    "flights": [
        r"\bflight\b", r"\bland(?:ing|ed)\b", r"\bdepart(?:ure|ed|ing)\b",
        r"\bairport\b", r"\bplane\b", r"\bETA\b", r"\bdelayed flight\b",
    ],
    "snapshots": [
        r"\bniro\w*", r"\bgarmin\b", r"\boura\b", r"\bsleep\b", r"\bhrv\b",
        r"\bnetwork status\b", r"\bunifi\b", r"\bpihole\b",
    ],
    "home": [
        r"\blight\b", r"\bswitch\b", r"\block\b", r"\bunlock\b",
        r"\btemp(erature)?\b", r"\bhome state\b", r"\bha\b",
    ],
    "meals": [
        r"\bmeal\b", r"\bdinner\b", r"\blunch\b", r"\brecipe\b",
        r"\bgrocery\b", r"\bcook\b",
    ],
    "email": [r"\bemail\b", r"\binbox\b", r"\bmail\b"],
    "cognitive": [
        r"\bresearch\b",
        r"\bstudy guide\b",
        r"\bdefer\b",
        r"\b(deep dive|look into|dig into|investigate)\b",
        r"\b(compare|comparison|versus|vs\.?).{0,50}\b(options|hotels|restaurants|flights|prices|plans)\b",
        r"\b(plan|planning|itinerary).{0,40}\b(trip|vacation|weekend|travel|route)\b",
        r"\b(find|recommend|suggest).{0,50}\b(hotel|restaurant|dentist|doctor|contractor|place|spot)\b",
        r"\b(best .{0,40}\b(in|near|around|for))\b",
        r"\b(options for|pros and cons)\b",
    ],
    "presence": [r"\bwhere is\b", r"\bwho('?s| is) home\b", r"\blocation\b"],
    "media": [r"\bsnap(shot)?\b", r"\bcamera\b", r"\bfrigate\b"],
    "network": [r"\bnetwork\b", r"\bdevice\b", r"\bwifi\b", r"\bmac\b"],
    "identity": [r"\bwho is\b", r"\bdevice\b.*\bname\b"],
}

_CHITCHAT_PATTERNS = [
    r"^(hi|hey|hello|yo|sup|thanks|thank you|thx|ok|okay|k|cool|nice|lol|haha|👍|❤️)\s*[!.?]*$",
    r"^(good morning|good night|gm|gn)\s*[!.?]*$",
    r"^how are you\??$",
    r"^what'?s up\??$",
]

_OPEN_TOOL_PATTERNS = [
    r"\b(what|when|where|who|how|can you|could you|please|remind|show|get|check|list)\b",
    r"\?",
    r"/\w+",
]


def looks_chitchat(user_message: str) -> bool:
    """Explicit social messages with no actionable intent.

    Ambiguous short messages (e.g. ``lock it``, ``route 1``, ``today``) are
    **not** chit-chat — they stay on the full or narrowed tool surface.
    """
    if not user_message:
        return False
    text = user_message.strip()
    if len(text) > 80:
        return False
    lower = text.lower()
    if any(re.search(p, lower) for p in _CHITCHAT_PATTERNS):
        return True
    if any(re.search(p, lower) for p in _OPEN_TOOL_PATTERNS):
        return False
    return False


def _should_strip_tools_for_chitchat(user_message: str) -> bool:
    """Strip tools only for explicit social messages with no domain in this turn."""
    if not looks_chitchat(user_message):
        return False
    return not _domains_matching_message(user_message)


def looks_deep_research_intent(text: str) -> bool:
    """True when the user likely needs async ResearchWorker (Ollama/deba), not inline web_search."""
    if not text:
        return False
    lower = text.lower()
    return any(re.search(p, lower) for p in _DOMAIN_PATTERNS["cognitive"])


def _domains_matching_message(text: str) -> set[str]:
    matched: set[str] = set()
    lower = text.lower()
    if looks_schedule_intent(text):
        matched.add("calendar")
    for domain, patterns in _DOMAIN_PATTERNS.items():
        if any(re.search(p, lower) for p in patterns):
            matched.add(domain)
    return matched


def _recent_user_text(history: list[dict] | None, max_turns: int = 2) -> str:
    if not history:
        return ""
    chunks: list[str] = []
    for msg in reversed(history):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            chunks.append(content)
        if len(chunks) >= max_turns:
            break
    return " ".join(reversed(chunks))


def narrow_tool_domains(
    *,
    mode_domains: list[str] | None,
    user_message: str,
    config: dict[str, Any],
    history: list[dict] | None = None,
    channel_id: str | None = None,
) -> list[str] | None:
    """Return narrowed domain allowlist (intent router) for ``get_tool_schemas``, or ``[]`` for text-only.

    This is the intent-narrowing step. The outer resolve (mode ceiling + channel + discovery)
    lives in tool_surface.resolve_tool_domains (Wave 1a+).
    """
    router_cfg = (config.get("context") or {}).get("intent_router") or {}
    if not router_cfg.get("enabled", False):
        return mode_domains

    if _channel_is(config, channel_id, "anvil"):
        return mode_domains

    if mode_domains is not None and len(mode_domains) == 0:
        return []

    ceiling = set(mode_domains) if mode_domains is not None else None

    combined = f"{_recent_user_text(history)} {user_message or ''}".strip()
    sticky = int(router_cfg.get("sticky_turns", 2))
    if _should_strip_tools_for_chitchat(user_message or ""):
        return []

    matched = _domains_matching_message(combined)
    if not matched:
        # 2wh.14: ambiguous (no domain match) — default still full mode allowlist.
        # Opt-in core-only: context.intent_router.ambiguous_core_only=true
        # (discovery tools stay always-on via tool_surface; not listed here).
        if router_cfg.get("ambiguous_core_only", False):
            core = set(_core_domains_for(config))
            if ceiling is not None:
                core &= ceiling
            return sorted(core) if core else (mode_domains if mode_domains is not None else [])
        return mode_domains

    if sticky > 0 and history:
        extra = _domains_matching_message(_recent_user_text(history, sticky))
        matched |= extra

    chosen = set(_core_domains_for(config)) | matched
    if ceiling is not None:
        chosen &= ceiling
    if not chosen and ceiling is not None:
        return mode_domains
    return sorted(chosen)


def active_surface_summary(domains: list[str] | None, tool_count: int) -> str:
    """Compatibility wrapper — delegates to the rich Wave 2 implementation in tool_surface.

    Old call sites (domains, count) continue to work.
    """
    from .tool_surface import active_surface_summary as _rich
    return _rich(domains, tool_count)
