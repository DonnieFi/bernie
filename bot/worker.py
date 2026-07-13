"""
Cognitive worker framework for Bernie's background task queue.

CognitiveWorker  — polls cognitive_tasks every 10s, runs handlers
WatchdogWorker   — runs every 60s, resets zombie tasks, fails dead ones
"""
import asyncio
import logging

from discord.ext import tasks

from telemetry import build_stats
from cognitive_handlers.registry import HANDLERS, get_handler, task_handler
from cognitive_handlers import worker_shared

# Register cognitive task handlers (side effect on import).
import cognitive_handlers.handlers  # noqa: F401

log = logging.getLogger("bernie.worker")

# Backward-compat re-exports for tests and cognitive_workers.
ANTHROPIC_KEY = worker_shared.ANTHROPIC_KEY
OLLAMA_SEMAPHORE = worker_shared.OLLAMA_SEMAPHORE
SMALL_MODEL_DISCIPLINE = worker_shared.SMALL_MODEL_DISCIPLINE
_handlers = HANDLERS

_get_ollama_semaphore = worker_shared._get_ollama_semaphore
_get_ollama_sem_for_tests = worker_shared._get_ollama_sem_for_tests
_reset_for_tests = worker_shared._reset_for_tests
_call_worker_model = worker_shared.call_worker_model
_call_ollama_fallback = worker_shared.call_ollama_fallback
_call_anthropic_topic = worker_shared.call_anthropic_topic
_call_ollama_topic = worker_shared.call_ollama_topic


# ── CognitiveWorker ───────────────────────────────────────────────────────────

class CognitiveWorker:
    """Polls cognitive_tasks every 10s and dispatches to registered handlers."""

    def __init__(self, container):
        self.container = container
        self._loop: "tasks.Loop | None" = None

    @property
    def loop(self) -> "tasks.Loop":
        if self._loop is None:
            raise RuntimeError("CognitiveWorker not registered with BTS. Call register_with_bts first.")
        return self._loop

    def register_with_bts(self, bts) -> "tasks.Loop":
        self._loop = bts.register(
            "cognitive_worker",
            self._poll,
            interval={"seconds": 10},
            owner="cognition",
            tier="immediate",
        )
        return self._loop

    async def _poll(self):
        db = self.container.db
        try:
            task = await db.claim_next_task()
            if not task:
                return
            task_id = task["id"]
            task_type = task["type"]
            log.info("CognitiveWorker: claimed task id=%d type=%s", task_id, task_type)

            handler = get_handler(task_type)
            if not handler:
                log.warning("CognitiveWorker: no handler for type=%s, failing task", task_type)
                await db.fail_cognitive_task(task_id, f"No handler for type '{task_type}'")
                return

            import json as _json

            _pl = task.get("payload") or {}
            if isinstance(_pl, str):
                _pl = _json.loads(_pl or "{}")
            _uid = _pl.get("unified_task_id")

            heartbeat_task = asyncio.create_task(self._heartbeat_loop(task_id))
            try:
                result = await handler(task, self.container)
                if isinstance(result, dict) and "_stats" in result:
                    stats = result.pop("_stats", {}) or {}
                    payload_result = result.pop("_result", None)
                    if payload_result is None and result:
                        payload_result = result
                    await db.complete_cognitive_task_with_stats(task_id, payload_result, stats)
                else:
                    stats = build_stats(model=None)
                    await db.complete_cognitive_task(task_id, result)

                try:
                    await db.log_token_usage(
                        input_tokens=stats.get("tokens_in", 0) or 0,
                        output_tokens=stats.get("tokens_out", 0) or 0,
                        model=stats.get("model") or "unknown",
                        triggered_by=f"cognitive_worker:{task_type}",
                        cache_creation_tokens=stats.get("cache_creation_tokens", 0) or 0,
                        cache_read_tokens=stats.get("cache_read_tokens", 0) or 0,
                    )
                except Exception:
                    log.exception("Failed to log token_usage for task %d", task_id)
                log.info("CognitiveWorker: completed task id=%d", task_id)
                if _uid:
                    _out = await db.get_task_output_by_key(f"research:{task_id}")
                    _content = (_out.get("content") if isinstance(_out, dict) else None) or str(result)[:500]
                    if self.container and hasattr(self.container, "unified_tasks") and self.container.unified_tasks:
                        await self.container.unified_tasks.finalize_research_task(
                            _uid,
                            ok=True,
                            summary=_content,
                            run_id=f"ct-{task_id}",
                            metrics=stats,
                            deliver=True,
                            container=self.container,
                        )
                    else:
                        from research_bridge import finalize_unified_from_research

                        await finalize_unified_from_research(
                            _uid,
                            ok=True,
                            summary=_content,
                            run_id=f"ct-{task_id}",
                            metrics=stats,
                            deliver=True,
                            task_store=self.container.task_store if self.container else None,
                            notification_router=(
                                self.container.notification_orchestrator if self.container else None
                            ),
                            container=self.container,
                        )
            except Exception as e:
                log.exception("CognitiveWorker: task id=%d failed", task_id)
                await db.fail_cognitive_task(task_id, str(e))
                if _uid:
                    import traceback as _tb

                    if self.container and hasattr(self.container, "unified_tasks") and self.container.unified_tasks:
                        await self.container.unified_tasks.finalize_research_task(
                            _uid,
                            ok=False,
                            summary="research failed",
                            run_id=f"ct-{task_id}",
                            error=str(e),
                            logs=_tb.format_exc(),
                        )
                    else:
                        from research_bridge import finalize_unified_from_research

                        await finalize_unified_from_research(
                            _uid,
                            ok=False,
                            summary="research failed",
                            run_id=f"ct-{task_id}",
                            error=str(e),
                            logs=_tb.format_exc(),
                            task_store=self.container.task_store if self.container else None,
                            notification_router=(
                                self.container.notification_orchestrator if self.container else None
                            ),
                        )
            finally:
                heartbeat_task.cancel()
        except Exception:
            log.exception("CognitiveWorker poll error")

    async def _heartbeat_loop(self, task_id: int):
        db = self.container.db
        try:
            while True:
                await asyncio.sleep(30)
                try:
                    await db.update_task_heartbeat(task_id)
                except Exception as e:
                    log.warning("Heartbeat update failed for task %d: %s", task_id, e)
        except asyncio.CancelledError:
            pass


# ── WatchdogWorker ────────────────────────────────────────────────────────────

class WatchdogWorker:
    """Runs every 60s. Resets zombie tasks; moves dead tasks to dead_letter."""

    def __init__(self, container=None):
        self.container = container
        self._loop: "tasks.Loop | None" = None

    @property
    def loop(self) -> "tasks.Loop":
        if self._loop is None:
            raise RuntimeError("WatchdogWorker not registered with BTS. Call register_with_bts first.")
        return self._loop

    def register_with_bts(self, bts) -> "tasks.Loop":
        self._loop = bts.register(
            "watchdog_worker",
            self._watch,
            interval={"seconds": 60},
            owner="cognition",
            tier="immediate",
        )
        return self._loop

    async def _watch(self):
        db = self.container.db if self.container else None
        if db is None:
            return
        try:
            stale = await db.get_stale_active_tasks(older_than_minutes=5)
            for task in stale:
                task_id = task["id"]
                log.warning(
                    "Watchdog: zombie task id=%d type=%s retry=%d/%d",
                    task_id,
                    task["type"],
                    task["retry_count"],
                    task["max_retries"],
                )
                await db.fail_cognitive_task(task_id, "heartbeat timeout (zombie)")
            reclaimed = await db.reclaim_stalled_unified_tasks(
                older_than_minutes=db.UNIFIED_RECLAIM_TIMEOUT_MINUTES,
            )
            if reclaimed:
                log.info("Watchdog: reclaimed %d stalled unified task(s): %s", len(reclaimed), reclaimed)
        except Exception:
            log.exception("WatchdogWorker error")


# ── Module-level singletons (created in main.py) ─────────────────────────────

_cognitive_worker: CognitiveWorker | None = None
_watchdog_worker: WatchdogWorker | None = None


def init_workers(container) -> tuple[CognitiveWorker, WatchdogWorker]:
    global _cognitive_worker, _watchdog_worker
    _cognitive_worker = CognitiveWorker(container)
    _watchdog_worker = WatchdogWorker(container)
    return _cognitive_worker, _watchdog_worker
