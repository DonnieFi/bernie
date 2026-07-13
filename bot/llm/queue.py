"""LiteLLM request queue + backpressure (P1).

Minimal implementation for the single PR. Depth, shed shadow first.
Real scheduling can be expanded later.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
import db_writes

log = logging.getLogger(__name__)


class LLMQueue:
    """Simple bounded queue for LLM calls. Slight lag OK under burst.
    Shed shadow first if configured.
    """

    def __init__(self, max_depth: int = 4, shed_shadow_first: bool = True):
        self.max_depth = max_depth
        self.shed_shadow_first = shed_shadow_first
        self._cond = asyncio.Condition()
        self._depth = 0

    async def configure(self, *, max_depth: int, shed_shadow_first: bool) -> None:
        async with self._cond:
            self.max_depth = max(1, int(max_depth or 1))
            self.shed_shadow_first = bool(shed_shadow_first)
            self._cond.notify_all()

    def _emit(self, action: str, *, shadow: bool, waited: bool = False) -> None:
        try:
            from telemetry import fire_and_forget
            from db_binding import get_database
            meta = json.dumps({
                "depth": self._depth,
                "max_depth": self.max_depth,
                "shadow": bool(shadow),
                "waited": bool(waited),
            })
            fire_and_forget(db_writes.routed("log_activity", 
                "llm_queue",
                f"llm_queue {action} depth={self._depth}/{self.max_depth}",
                meta=meta,
            ))
        except Exception:
            pass

    async def acquire(self, *, shadow: bool = False) -> bool:
        """Acquire slot. Return False if shed."""
        async with self._cond:
            if shadow and self.shed_shadow_first and self._depth >= self.max_depth:
                log.debug("llm queue shed shadow (depth=%s max=%s)", self._depth, self.max_depth)
                self._emit("shed", shadow=shadow)
                return False

            waited = self._depth >= self.max_depth
            if waited:
                self._emit("queued", shadow=shadow, waited=True)
            while self._depth >= self.max_depth:
                await self._cond.wait()
            self._depth += 1
            self._emit("acquired", shadow=shadow, waited=waited)
            return True

    def release(self) -> None:
        """Legacy no-op; use slot() so release can notify async waiters."""

    @asynccontextmanager
    async def slot(self, *, shadow: bool = False):
        got = await self.acquire(shadow=shadow)
        if not got:
            raise RuntimeError("shed")
        try:
            yield
        finally:
            async with self._cond:
                self._depth = max(0, self._depth - 1)
                self._emit("released", shadow=shadow)
                self._cond.notify_all()

    @property
    def depth(self) -> int:
        return self._depth


# default instance (config can override)
_default_queue: LLMQueue | None = None


async def queued_run(coro, app_config: dict, *, shadow: bool = False, shed_shadow_first: bool | None = None):
    """Run any awaitable through the default LLM queue + step timeout."""
    import asyncio

    q = get_default_queue()
    exec_cfg = app_config.get("executor", {})
    if shed_shadow_first is None:
        from eval.policy import resolve_eval_policy
        shed_shadow_first = resolve_eval_policy(app_config).shed_on_backpressure
    await q.configure(
        max_depth=int(exec_cfg.get("llm_queue_max_depth", 4)),
        shed_shadow_first=shed_shadow_first,
    )
    timeout_s = float(exec_cfg.get("llm_step_timeout_s", 45))
    async with q.slot(shadow=shadow):
        return await asyncio.wait_for(coro, timeout=timeout_s)


async def queued_messages_create(
    client,
    app_config: dict,
    *,
    shadow: bool = False,
    **kwargs,
):
    """Run client.messages.create through the default LLM queue + step timeout."""
    from model_registry import model_source
    from openrouter_models import resolve_openrouter_slug

    model = kwargs.get("model")
    if model and model_source(model, app_config) == "openrouter":
        kwargs = {**kwargs, "model": resolve_openrouter_slug(model, app_config)}

    return await queued_run(
        client.messages.create(**kwargs),
        app_config,
        shadow=shadow,
    )


def get_default_queue() -> LLMQueue:
    global _default_queue
    if _default_queue is None:
        _default_queue = LLMQueue()
    return _default_queue
