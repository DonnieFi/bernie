"""BackgroundTaskScheduler — unified registry wrapping TaskSupervisor.

Replaces the (decorator + sv.register + sv.update_health) three-step per
task with a single bts.register() call. Crash handling delegates to the
existing TaskSupervisor (3 restarts then #anvil alert).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Coroutine

from discord.ext import tasks as _discord_tasks

from supervisor import TaskSupervisor

log = logging.getLogger(__name__)

# Task owner tags (per-container). ROLE=monolith is a runtime mode that starts
# every owner — used for local dev and docker-compose.monolith.yml rollback.
VALID_BTS_OWNERS = frozenset({"discord", "api", "cognition"})
ROLE_MONOLITH = "monolith"

_scheduler: "BackgroundTaskScheduler | None" = None


def timing_snapshot(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Snapshot BTS timing keys for before/after config reload comparisons."""
    from config import config as live_config

    c = live_config if cfg is None else cfg
    return {
        "summary_hour": c.get("summary_hour", 7),
        "summary_minute": c.get("summary_minute", 0),
        "weekly_summary_hour": c.get("weekly_summary_hour", 20),
        "weekly_summary_minute": c.get("weekly_summary_minute", 0),
        "poll_interval_minutes": c.get("poll_interval_minutes"),
        "network_watchman_poll_minutes": c.get("network_watchman", {}).get(
            "poll_interval_minutes", 15
        ),
    }


class BackgroundTaskScheduler:
    """Registers async functions as discord.ext.tasks.Loop with metadata."""

    def __init__(self, supervisor: TaskSupervisor):
        self._supervisor = supervisor
        self._meta: dict[str, dict[str, Any]] = {}
        self._task_stats: dict[str, dict] = {}  # last known stats per task name

    def register(
        self,
        name: str,
        func: Callable[..., Coroutine],
        *,
        interval: dict[str, Any],
        owner: str = "discord",
        tier: str = "immediate",
        restart_policy: str = "always",
    ) -> _discord_tasks.Loop:
        """Wrap func in a tasks.Loop, register with supervisor, store metadata.

        interval: keyword args forwarded to discord.ext.tasks.loop(), e.g.
            {"minutes": 5}, {"seconds": 10}, {"time": time(hour=9, ...)}.
        owner: which container this task belongs to (discord/api/cognition).
        tier: scheduling priority — immediate / can-defer / overnight-only.
        restart_policy: always / on-failure / never (informational for now).
        """
        if owner not in VALID_BTS_OWNERS:
            log.warning(
                "BTS task %r has unknown owner=%r (expected one of %s)",
                name,
                owner,
                sorted(VALID_BTS_OWNERS),
            )
        sv = self._supervisor

        async def _wrapped():
            try:
                # Keep the success-path update_health OUTSIDE the func() except,
                # so a bug *inside* update_health on success is NOT misattributed
                # as a task failure (which would trigger a spurious restart).
                try:
                    await func()
                except Exception as exc:
                    sv.update_health(name, error=exc)
                else:
                    sv.update_health(name)
            finally:
                # Always touch the heartbeat, even on transient failures.
                # This prevents Docker from restarting the cognition container
                # during short DB lock storms or network hiccups.
                try:
                    open("/tmp/bts_heartbeat", "w").close()
                except Exception:
                    pass

        _wrapped.__name__ = func.__name__ if hasattr(func, "__name__") else name
        loop = _discord_tasks.loop(**interval)(_wrapped)
        sv.register(name, loop)
        self._meta[name] = {
            "interval": interval,
            "owner": owner,
            "tier": tier,
            "restart_policy": restart_policy,
        }
        log.debug("BTS registered task '%s' (owner=%s, tier=%s)", name, owner, tier)
        return loop

    def change_interval(self, name: str, **interval_kwargs: Any) -> None:
        """Reschedule a registered task (discord.ext.tasks.Loop.change_interval)."""
        loop = self._supervisor.tasks.get(name)
        if loop is None:
            raise KeyError(f"BTS task '{name}' not registered")
        loop.change_interval(**interval_kwargs)
        if name in self._meta:
            self._meta[name]["interval"] = dict(interval_kwargs)
        log.info("BTS rescheduled task '%s': %s", name, interval_kwargs)

    def sync_intervals_from_config(self, prior: dict[str, Any]) -> list[str]:
        """Reschedule BTS loops when timing-related config keys change after reload."""
        from datetime import time

        from config import TASK_TZ, config

        updated: list[str] = []

        sh = config.get("summary_hour", 7)
        sm = config.get("summary_minute", 0)
        if prior.get("summary_hour") != sh or prior.get("summary_minute") != sm:
            self.change_interval(
                "daily_summary",
                time=time(hour=sh, minute=sm, tzinfo=TASK_TZ),
            )
            updated.append("daily_summary")

        wh = config.get("weekly_summary_hour", 20)
        wm = config.get("weekly_summary_minute", 0)
        if prior.get("weekly_summary_hour") != wh or prior.get("weekly_summary_minute") != wm:
            self.change_interval(
                "weekly_summary",
                time=time(hour=wh, minute=wm, tzinfo=TASK_TZ),
            )
            updated.append("weekly_summary")

        poll = config.get("poll_interval_minutes")
        if prior.get("poll_interval_minutes") != poll and poll is not None:
            self.change_interval("reminders", minutes=poll)
            updated.append("reminders")

        nw_poll = config.get("network_watchman", {}).get("poll_interval_minutes", 15)
        if prior.get("network_watchman_poll_minutes") != nw_poll:
            self.change_interval("network_watchman", minutes=nw_poll)
            updated.append("network_watchman")

        return updated

    async def start_all(self) -> None:
        role = os.environ.get("ROLE", ROLE_MONOLITH)
        only_names = None
        # Monolith starts every registered task (rollback / single-container dev).
        if role != ROLE_MONOLITH:
            only_names = {
                name
                for name in self._supervisor.tasks
                if self._meta.get(name, {}).get("owner", "discord") == role
            }
            log.info(
                "BTS start_all: role=%s starting %d/%d tasks",
                role,
                len(only_names),
                len(self._supervisor.tasks),
            )
        await self._supervisor.start_all(only_names=only_names)

    def get_status(self) -> dict[str, Any]:
        """Supervisor health + BTS metadata merged per task."""
        status = self._supervisor.get_status()
        for name, meta in self._meta.items():
            if name in status.get("tasks", {}):
                status["tasks"][name].update(meta)
                if name in self._task_stats:
                    status["tasks"][name]["last_stats"] = self._task_stats[name]
        return status

    def record_task_stats(self, name: str, stats: dict) -> None:
        """Called by workers after a task completes to persist cost/usage info."""
        self._task_stats[name] = stats or {}


def init_scheduler(supervisor: TaskSupervisor) -> BackgroundTaskScheduler:
    global _scheduler
    _scheduler = BackgroundTaskScheduler(supervisor)
    return _scheduler


def get_scheduler() -> BackgroundTaskScheduler:
    if _scheduler is None:
        raise RuntimeError(
            "BackgroundTaskScheduler not initialized. Call init_scheduler first."
        )
    return _scheduler
