import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import discord
from discord.ext import tasks

log = logging.getLogger("bernie.supervisor")

_MAX_RESTARTS = 3  # Consecutive failures before giving up and alerting #anvil


class TaskSupervisor:
    """Central registry and manager for Bernie's background loops."""

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.tasks: Dict[str, tasks.Loop] = {}
        self._health: Dict[str, Dict[str, Any]] = {}
        self._restart_counts: Dict[str, int] = {}
        self._started = False
        self._alert_tasks: set = set()

    def register(self, name: str, loop: tasks.Loop):
        """Register a discord.ext.tasks.Loop. Idempotent — safe to call on Discord reconnect."""
        if name in self.tasks:
            # Reconnect path: update loop reference but preserve health history
            self.tasks[name] = loop
            log.debug(f"Supervisor: Re-registered task '{name}' (health preserved)")
            return
        self.tasks[name] = loop
        self._health[name] = {
            "status": "registered",
            "last_run": None,
            "run_count": 0,
            "errors": 0,
            "last_error": None,
        }
        self._restart_counts[name] = 0
        log.info(f"Supervisor: Registered task '{name}'")

    async def start_all(self, only_names: set[str] | None = None):
        """Start registered tasks that aren't already running.

        When only_names is set, tasks not in the set are skipped (40A-2 owner
        filter). Safe to call on Discord reconnect.
        """
        if self._started:
            log.info("Supervisor: Reconnect detected — restarting any stopped loops.")
            for name, loop in self.tasks.items():
                if only_names is not None and name not in only_names:
                    continue
                if not loop.is_running():
                    try:
                        loop.start()
                        self._health[name]["status"] = "running"
                        log.info(f"Supervisor: Restarted stopped task '{name}'")
                    except Exception as e:
                        self._health[name]["status"] = "failed_to_start"
                        self._health[name]["last_error"] = str(e)
                        log.error(f"Supervisor: Failed to restart task '{name}': {e}")
            return

        log.info("Supervisor: Starting all background loops...")
        for name, loop in self.tasks.items():
            if only_names is not None and name not in only_names:
                log.debug("Supervisor: Skipping task '%s' (owner filter)", name)
                continue
            if not loop.is_running():
                try:
                    loop.start()
                    self._health[name]["status"] = "running"
                    log.info(f"Supervisor: Started task '{name}'")
                except Exception as e:
                    self._health[name]["status"] = "failed_to_start"
                    self._health[name]["last_error"] = str(e)
                    log.error(f"Supervisor: Failed to start task '{name}': {e}")

        self._started = True
        log.info("Supervisor: All systems go.")

    def update_health(self, name: str, error: Optional[Exception] = None):
        """Update health metrics for a task. Called by loops themselves."""
        if name not in self._health:
            return

        stats = self._health[name]
        stats["last_run"] = datetime.now(timezone.utc).isoformat()
        stats["run_count"] += 1

        if error:
            stats["errors"] += 1
            stats["last_error"] = str(error)
            rc = self._restart_counts.get(name, 0) + 1
            self._restart_counts[name] = rc
            loop = self.tasks.get(name)

            if loop and rc <= _MAX_RESTARTS:
                try:
                    loop.restart()
                    stats["status"] = "restarting"
                    log.warning(f"Supervisor: Auto-restarting '{name}' (attempt {rc}/{_MAX_RESTARTS})")
                except Exception as re:
                    stats["status"] = "degraded"
                    log.error(f"Supervisor: Could not restart '{name}': {re}")
            else:
                stats["status"] = "degraded"
                if rc == _MAX_RESTARTS + 1:  # Alert only once per failure streak
                    task = asyncio.create_task(self._alert_anvil(name, str(error), rc))
                    self._alert_tasks.add(task)
                    task.add_done_callback(self._alert_tasks.discard)
        else:
            stats["status"] = "running"
            self._restart_counts[name] = 0  # Reset streak on success

    async def _alert_anvil(self, name: str, last_error: str, fail_count: int):
        """Post to #anvil when a task exceeds max restart attempts."""
        from config import config
        anvil_id = config.get("anvil_channel_id")
        if not anvil_id:
            log.warning(f"Supervisor: No anvil_channel_id — can't alert for '{name}'")
            return
        body = (
            f"⚠️ **Supervisor Alert**: Task `{name}` has failed {fail_count} times "
            f"and will no longer be auto-restarted.\n"
            f"Last error: `{last_error}`\n"
            f"Restart Bernie to resume this task."
        )
        try:
            from cross_container import post_to_anvil

            await post_to_anvil(body, bot=self.bot, config=config)
        except Exception as e:
            log.error(f"Supervisor: Failed to send #anvil alert for '{name}': {e}")

    def get_status(self) -> Dict[str, Any]:
        """Return health status merged with live loop state."""
        result = {}
        for name, loop in self.tasks.items():
            health = dict(self._health.get(name, {}))
            health["is_running"] = loop.is_running()
            # loop.failed() is always False for BTS-wrapped loops because the
            # wrapper catches and reports exceptions to update_health. Fall
            # back to health state so /api/scheduler still surfaces failures.
            health["failed"] = bool(loop.failed()) or health.get("status") == "degraded"
            health["current_loop"] = loop.current_loop
            if not loop.is_running() and health.get("status") == "running":
                health["status"] = "stopped"
            result[name] = health
        return {"started": self._started, "tasks": result}


# Singleton
supervisor: Optional[TaskSupervisor] = None


def init_supervisor(bot: discord.Client) -> TaskSupervisor:
    global supervisor
    supervisor = TaskSupervisor(bot)
    return supervisor


def get_supervisor() -> TaskSupervisor:
    if supervisor is None:
        raise RuntimeError("Supervisor not initialized. Call init_supervisor first.")
    return supervisor
