"""Weekly Curator — prune-only routines + semantic_observations (family-bot-5hy.8).

NO DSPy/GEPA — confidence + staleness only. Posts a short #anvil report.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("bot")

DEFAULT_MAX_CONFIDENCE = 0.3
DEFAULT_STALE_DAYS = 30


def is_prune_candidate(
    confidence: float | None,
    observed_or_created_iso: str | None,
    *,
    now: datetime | None = None,
    max_confidence: float = DEFAULT_MAX_CONFIDENCE,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> bool:
    """Pure filter: confidence < threshold AND timestamp older than stale_days."""
    try:
        conf = float(confidence) if confidence is not None else 1.0
    except (TypeError, ValueError):
        conf = 1.0
    if conf >= max_confidence:
        return False
    if not observed_or_created_iso:
        return False
    raw = str(observed_or_created_iso).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return ts < (ref - timedelta(days=max(1, int(stale_days))))


def format_curator_report(routines: list[dict], observations: list[dict]) -> str | None:
    """Short #anvil summary. None when nothing was pruned."""
    n_r = len(routines)
    n_o = len(observations)
    if n_r == 0 and n_o == 0:
        return None
    lines = [
        f"🧹 **Weekly Curator** — pruned {n_r} routine(s), {n_o} observation(s)",
        f"_(confidence < {DEFAULT_MAX_CONFIDENCE} and stale > {DEFAULT_STALE_DAYS}d)_",
    ]
    for r in routines[:8]:
        pid = r.get("person_id") or "?"
        name = r.get("name") or "?"
        conf = r.get("confidence")
        conf_s = f"{float(conf):.2f}" if conf is not None else "?"
        lines.append(f"• routine `{pid}` / {name} (conf {conf_s})")
    if n_r > 8:
        lines.append(f"• … +{n_r - 8} more routine(s)")
    for o in observations[:8]:
        pid = o.get("person_id") or "?"
        text = (o.get("observation") or "")[:60]
        conf = o.get("confidence")
        conf_s = f"{float(conf):.2f}" if conf is not None else "?"
        lines.append(f"• obs `{pid}`: {text} (conf {conf_s})")
    if n_o > 8:
        lines.append(f"• … +{n_o - 8} more observation(s)")
    return "\n".join(lines)


async def run_weekly_curator(*, bot: Any = None, config: dict | None = None) -> dict:
    """Prune stale low-confidence memory rows; report to #anvil when anything deleted."""
    import db_writes

    cfg = config if config is not None else {}
    routines = await db_writes.routed(
        "prune_stale_low_confidence_routines",
        max_confidence=DEFAULT_MAX_CONFIDENCE,
        stale_days=DEFAULT_STALE_DAYS,
    )
    observations = await db_writes.routed(
        "prune_stale_low_confidence_observations",
        max_confidence=DEFAULT_MAX_CONFIDENCE,
        stale_days=DEFAULT_STALE_DAYS,
    )
    routines = routines or []
    observations = observations or []

    report = format_curator_report(routines, observations)
    if report:
        try:
            from cross_container import post_to_anvil

            await post_to_anvil(report, bot=bot, config=cfg)
        except Exception as e:
            log.error("curator: #anvil post failed: %s", e, exc_info=True)
        try:
            await db_writes.routed(
                "log_activity",
                event_type="curator_prune",
                description=(
                    f"pruned {len(routines)} routines, "
                    f"{len(observations)} observations"
                ),
            )
        except Exception:
            log.debug("curator: activity_log write failed", exc_info=True)

    log.info(
        "curator: pruned %d routine(s), %d observation(s)",
        len(routines),
        len(observations),
    )
    return {
        "ok": True,
        "routines_pruned": len(routines),
        "observations_pruned": len(observations),
    }


async def weekly_curator_task():
    """BTS entry — Sunday prune pass (weekday gate mirrors routine_decay)."""
    from config import TASK_TZ, config

    if datetime.now(TASK_TZ).weekday() != 6:  # Sunday
        return
    try:
        await run_weekly_curator(bot=None, config=config)
    except Exception as e:
        log.error("weekly_curator_task error: %s", e, exc_info=True)
        raise
