"""BackgroundTaskScheduler registration tables (family-bot-8lx.3).

Task *bodies* stay in bot.py (or domain modules); this file only owns the
register() table so bot.on_ready stays smaller and ownership is visible.
"""
from __future__ import annotations

import os
from datetime import time
from typing import Any

from config import TASK_TZ, config


def register_cognition_bts_tasks(bts: Any, m: Any) -> None:
    """Register BTS loops that run on bernie-cognition only (40A-2)."""
    bts.register(
        "sqlite_backup",
        m.sqlite_backup_task,
        interval={"time": time(hour=3, minute=45, tzinfo=TASK_TZ)},
        owner="cognition",
        tier="overnight-only",
    )
    bts.register(
        "ollama_overnight_preflight",
        m.ollama_overnight_preflight_task,
        interval={"time": time(hour=2, minute=5, tzinfo=TASK_TZ)},
        owner="cognition",
        tier="overnight-only",
    )
    bts.register(
        "nightly_eval",
        m.nightly_eval_task,
        interval={"time": time(hour=2, minute=30, tzinfo=TASK_TZ)},
        owner="cognition",
        tier="overnight-only",
    )
    bts.register(
        "reflection_enqueue",
        m.reflection_enqueue_task,
        interval={"time": time(hour=2, minute=15, tzinfo=TASK_TZ)},
        owner="cognition",
        tier="overnight-only",
    )
    bts.register(
        "consolidation_enqueue",
        m.consolidation_enqueue_task,
        interval={"time": time(hour=4, minute=0, tzinfo=TASK_TZ)},
        owner="cognition",
        tier="overnight-only",
    )
    bts.register(
        "routine_decay",
        m.routine_decay_task,
        interval={"time": time(hour=4, minute=0, tzinfo=TASK_TZ)},
        owner="cognition",
        tier="overnight-only",
    )
    bts.register(
        "dead_letter_digest",
        m.dead_letter_digest_task,
        interval={"time": time(hour=4, minute=30, tzinfo=TASK_TZ)},
        owner="cognition",
        tier="overnight-only",
    )
    bts.register(
        "db_wal_checkpoint",
        m.db_wal_checkpoint_task,
        interval={"minutes": 30},
        owner="cognition",
        tier="can-defer",
    )


def register_discord_bts_tasks(bts: Any, m: Any) -> None:
    """Register BTS loops owned by bernie-discord / monolith."""
    bts.register(
        "reminders",
        m.check_reminders,
        interval={"minutes": config["poll_interval_minutes"]},
        owner="discord",
        tier="immediate",
    )
    bts.register(
        "daily_summary",
        m.daily_summary_task,
        interval={
            "time": time(
                hour=config.get("summary_hour", 7),
                minute=config.get("summary_minute", 0),
                tzinfo=TASK_TZ,
            )
        },
        owner="discord",
        tier="immediate",
    )
    bts.register(
        "weekly_summary",
        m.weekly_summary_task,
        interval={
            "time": time(
                hour=config.get("weekly_summary_hour", 20),
                minute=config.get("weekly_summary_minute", 0),
                tzinfo=TASK_TZ,
            )
        },
        owner="discord",
        tier="immediate",
    )
    bts.register(
        "weather_prefetch",
        m.weather_prefetch_task,
        interval={"time": time(hour=5, minute=0, tzinfo=TASK_TZ)},
        owner="discord",
        tier="can-defer",
    )
    bts.register(
        "live_snapshot",
        m.live_snapshot_task,
        interval={"minutes": 5},
        owner="discord",
        tier="can-defer",
    )
    from transit_discord import transit_zones_weekly_refresh

    bts.register(
        "transit_zones",
        transit_zones_weekly_refresh,
        interval={"hours": 168},
        owner="discord",
        tier="can-defer",
    )
    bts.register(
        "litellm_model_sync",
        m.litellm_model_sync_task,
        interval={"hours": 168},
        owner="discord",
        tier="can-defer",
    )
    bts.register(
        "ha_registry",
        m.ha_registry_refresh_task,
        interval={"time": time(hour=3, minute=0, tzinfo=TASK_TZ)},
        owner="discord",
        tier="overnight-only",
    )
    bts.register(
        "watchman",
        m.watchman_audit_task,
        interval={"time": time(hour=3, minute=0, tzinfo=TASK_TZ)},
        owner="discord",
        tier="overnight-only",
    )
    bts.register(
        "memory_prune",
        m.memory_prune_task,
        interval={"time": time(hour=1, minute=0, tzinfo=TASK_TZ)},
        owner="discord",
        tier="overnight-only",
    )
    bts.register(
        "quiet_hours_flush",
        m.quiet_hours_flush_task,
        interval={
            "time": time(
                hour=config.get("quiet_hours", {}).get("end_hour", 7),
                minute=1,
                tzinfo=TASK_TZ,
            )
        },
        owner="discord",
        tier="immediate",
    )
    bts.register(
        "personal_tasks",
        m.personal_tasks_task,
        interval={"minutes": 1},
        owner="discord",
        tier="immediate",
    )
    bts.register(
        "hitl_expiry",
        m.hitl_expiry_task,
        interval={"seconds": 60},
        owner="discord",
        tier="immediate",
    )
    bts.register(
        "hitl_purge",
        m.hitl_purge_task,
        interval={"time": time(hour=3, minute=15, tzinfo=TASK_TZ)},
        owner="discord",
        tier="overnight-only",
    )
    bts.register(
        "proactive_nudge",
        m.proactive_nudge_task,
        interval={"hours": 1},
        owner="discord",
        tier="can-defer",
    )
    bts.register(
        "inbox_ingest",
        m.inbox_ingest_task,
        interval={"hours": 1},
        owner="discord",
        tier="can-defer",
    )
    bts.register(
        "email_pending_expiry",
        m.email_pending_expiry_task,
        interval={"hours": 1},
        owner="discord",
        tier="can-defer",
    )
    bts.register(
        "email_send_rate_prune",
        m.email_send_rate_prune_task,
        interval={"hours": 1},
        owner="discord",
        tier="overnight-only",
    )
    bts.register(
        "network_monitor",
        m.network_monitor_task,
        interval={"minutes": 5},
        owner="discord",
        tier="immediate",
    )
    bts.register(
        "network_watchman",
        m.network_watchman_task,
        interval={
            "minutes": config.get("network_watchman", {}).get(
                "poll_interval_minutes", 15
            )
        },
        owner="discord",
        tier="can-defer",
    )
    bts.register(
        "study_scan",
        m.study_scan_task,
        interval={"minutes": 10},
        owner="discord",
        tier="immediate",
    )
    bts.register(
        "study_nightly_sweep",
        m.study_nightly_sweep_task,
        interval={"time": time(hour=5, minute=0, tzinfo=TASK_TZ)},
        owner="discord",
        tier="overnight-only",
    )
    bts.register(
        "weekly_cognitive_report",
        m.weekly_cognitive_report_task,
        interval={"time": time(hour=9, minute=0, tzinfo=TASK_TZ)},
        owner="discord",
        tier="can-defer",
    )

    from jobs.external_ip import external_ip_check_task
    from jobs.curator import weekly_curator_task

    bts.register(
        "external_ip_check",
        external_ip_check_task,
        interval={"time": time(hour=6, minute=15, tzinfo=TASK_TZ)},
        owner="discord",
        tier="overnight-only",
    )
    bts.register(
        "weekly_curator",
        weekly_curator_task,
        interval={"time": time(hour=3, minute=20, tzinfo=TASK_TZ)},
        owner="discord",
        tier="overnight-only",
    )

    # Cognition-owned overnight tasks on monolith only (not bernie-discord).
    if os.environ.get("ROLE", "monolith") == "monolith":
        register_cognition_bts_tasks(bts, m)
