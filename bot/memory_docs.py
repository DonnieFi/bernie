"""Hot memory docs helpers — char caps + USER_OVERRIDE (family-bot-5hy.4/5)."""
from __future__ import annotations

import logging
import pathlib
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CONTEXT_MAX = 12_000
DEFAULT_PERSON_MAX = 8_000
USER_OVERRIDE_NAMES = (
    "USER_OVERRIDE.md",
    "user_override.md",
    "DAD_OVERRIDE.md",  # legacy local name
)


def hot_memory_cfg(config: dict | None) -> dict[str, Any]:
    cfg = config or {}
    hm = cfg.get("hot_memory")
    if not isinstance(hm, dict):
        hm = (cfg.get("context") or {}).get("hot_memory") or {}
    if not isinstance(hm, dict):
        hm = {}
    return hm


def context_max_chars(config: dict | None = None) -> int:
    hm = hot_memory_cfg(config)
    return max(500, int(hm.get("context_md_max_chars", DEFAULT_CONTEXT_MAX)))


def person_max_chars(config: dict | None = None) -> int:
    hm = hot_memory_cfg(config)
    return max(500, int(hm.get("person_md_max_chars", DEFAULT_PERSON_MAX)))


def resolve_user_override_path(docs_root: pathlib.Path, config: dict | None = None) -> pathlib.Path | None:
    hm = hot_memory_cfg(config)
    custom = hm.get("user_override_path") or hm.get("override_filename")
    if custom:
        p = pathlib.Path(custom)
        if not p.is_absolute():
            p = docs_root / p
        return p if p.exists() else (docs_root / pathlib.Path(custom).name)
    for name in USER_OVERRIDE_NAMES:
        p = docs_root / name
        if p.exists():
            return p
    return docs_root / "USER_OVERRIDE.md"


def read_user_override(docs_root: pathlib.Path, config: dict | None = None) -> str:
    path = resolve_user_override_path(docs_root, config)
    if path is None or not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        log.warning("USER_OVERRIDE read failed: %s", e)
        return ""
    return text


def is_override_path(path: pathlib.Path, docs_root: pathlib.Path, config: dict | None = None) -> bool:
    """True if path is the immutable override file (agent must not write it)."""
    try:
        target = path.resolve()
    except OSError:
        target = path
    ov = resolve_user_override_path(docs_root, config)
    if ov is None:
        return path.name.upper() in {n.upper() for n in USER_OVERRIDE_NAMES}
    try:
        return target == ov.resolve()
    except OSError:
        return path.name == ov.name


def append_fact_with_cap(
    path: pathlib.Path,
    fact: str,
    *,
    max_chars: int,
) -> tuple[str, bool]:
    """Append fact; consolidate older body if over budget.

    Returns (status_message, consolidated).
    Never silently truncates the new fact beyond a 500-char hard line limit
    (callers should pre-strip).
    """
    fact = fact.replace("\n", " ").strip()
    if not fact:
        return "Empty fact — nothing written.", False
    if len(fact) > 500:
        fact = fact[:500].rstrip()

    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as e:
        return f"Could not read {path.name}: {e}", False

    candidate = (existing.rstrip() + "\n" + fact + "\n") if existing.strip() else (fact + "\n")
    if len(candidate) <= max_chars:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(candidate, encoding="utf-8")
        return "Context updated.", False

    # Consolidate: keep a marker + trailing portion of existing + new fact
    header = (
        f"(consolidated — older facts dropped to stay under {max_chars} chars)\n"
    )
    budget = max_chars - len(header) - len(fact) - 2
    if budget < 50:
        # Extreme: new fact only (may truncate)
        body = (fact + "\n")[:max_chars]
        path.write_text(body, encoding="utf-8")
        return (
            f"Context updated (consolidated to fit {max_chars}-char budget; only new fact kept).",
            True,
        )

    # Prefer keeping a trailing slice of existing facts; do not drop the sole line
    # when split("\n", 1)[-1] would be empty (single-line tail ending in newline).
    keep = max(budget, 50) if budget < 200 else budget
    tail = existing[-keep:] if existing else ""
    if "\n" in tail:
        _partial, rest = tail.split("\n", 1)
        if rest:
            tail = rest
        # else: single-line (or newline-terminated) slice — keep as-is
    body = header + tail.rstrip() + "\n" + fact + "\n"
    if len(body) > max_chars:
        # Prefer keeping the new fact at the end
        overflow = len(body) - max_chars
        if overflow > 0 and len(tail) > overflow:
            tail = tail[overflow:]
            if "\n" in tail:
                _p, rest = tail.split("\n", 1)
                if rest:
                    tail = rest
            body = header + tail.rstrip() + "\n" + fact + "\n"
        body = body[:max_chars]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return (
        f"Context updated (consolidated older facts to stay under {max_chars} chars).",
        True,
    )


async def maybe_warn_anvil(ctx, message: str) -> None:
    """Best-effort #anvil note when hot memory consolidates."""
    try:
        from config import config
        channel_id = config.get("anvil_channel_id")
        if not channel_id:
            return
        router = getattr(getattr(ctx, "services", None), "notification_orchestrator", None)
        if router is None:
            return
        notify = getattr(router, "notify", None)
        if not callable(notify):
            return
        await notify(
            message=f"⚠️ Hot memory: {message}",
            channel_id=int(channel_id),
            priority="normal",
        )
    except Exception as e:
        log.debug("hot memory anvil warn skipped: %s", e)
