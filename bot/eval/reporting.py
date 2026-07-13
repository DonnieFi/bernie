import logging
from db_binding import get_database

log = logging.getLogger(__name__)

def build_weekly_cognitive_summary(stats: list[dict], days: int = 7) -> str:
    """Format a code-fenced ASCII table digest for #anvil."""
    if not stats:
        return f"_No cognitive worker runs in the last {days} days._"
    lines = [f"**Cognitive Workers — last {days} days**", "```"]
    lines.append(f"{'worker':<16} {'runs':>5} {'ok':>5} {'avg dur':>9} {'cost':>8} {'tok in':>8} {'tok out':>8}")
    for s in stats:
        runs = s.get("runs") or 0
        done = s.get("done") or 0
        rate = int(100 * done / runs) if runs else 0
        avg_ms = int(s.get("avg_duration_ms") or 0)
        avg_str = f"{avg_ms / 1000:.1f}s" if avg_ms < 60_000 else f"{avg_ms / 60_000:.1f}m"
        cost_val = s.get("total_cost_usd") or 0.0
        cost_str = f"${cost_val:.4f}" if cost_val > 0 else "$0.00"
        lines.append(
            f"{s['type']:<16} {runs:>5} {rate:>4}% {avg_str:>9} {cost_str:>8} "
            f"{s.get('total_tokens_in') or 0:>8} {s.get('total_tokens_out') or 0:>8}"
        )
    lines.append("```")
    return "\n".join(lines)


async def weekly_cognitive_report_worker(config: dict, notification_router=None, orchestrator=None) -> None:
    """Post a per-worker digest to #anvil. Caller schedules Sunday 09:00."""
    notification_orchestrator = orchestrator or notification_router
    stats = await get_database().get_cognitive_stats(days=7)
    body = build_weekly_cognitive_summary(stats, days=7)
    anvil_id = config.get("anvil_channel_id") or config.get("admin_channel_id")
    if not anvil_id:
        log.warning("weekly_cognitive_report: missing anvil channel id")
        return

    posted = False
    if notification_orchestrator:
        try:
            results = await notification_orchestrator.notify(notification_orchestrator.notification(
                recipient_id=str(anvil_id),
                message=body,
                urgency="silent",
            ))
            posted = bool(results.get("discord"))
        except Exception:
            pass

    if not posted:
        # Fallback for cognition container
        try:
            from cross_container import post_to_discord
            await post_to_discord(int(anvil_id), content=body)
        except Exception:
            log.exception("weekly_cognitive_report: failed to post to #anvil (fallback also failed)")
