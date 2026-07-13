"""Turn timing instrumentation (perf P0 🔬).

Lightweight async context manager for end-to-end turn measurement.
Records phase marks (setup, context, llm, tools, send) using monotonic time.
On exit, logs a single 'turn_timing' row via database public API (fire-and-forget).

Usage (no added tail latency on hot path):
    from llm.turn_timer import TurnTimer
    from telemetry import fire_and_forget

    async with TurnTimer(
        turn_id=turn_id,
        channel_id=channel_id,
        person_id=person_id,
    ) as t:
        # ... build session/history ...
        t.mark("setup")
        # ... build_context ...
        t.mark("context")
        ...
    # on exit (even on exception) the timing row is enqueued

All writes are deferred; the context manager itself does almost zero work.
Reversible: completely additive, gated by nothing — can be left in or
wrapped behind a config flag later if desired.
"""

from __future__ import annotations

import time
import logging
import contextvars
from typing import Optional

from telemetry import fire_and_forget
import db_writes

log = logging.getLogger(__name__)

# ContextVar so deep calls (build_context, native tool loops) can discover the
# active timer for phase recording and turn_id without threading everywhere yet.
_current_timer: contextvars.ContextVar["TurnTimer | None"] = contextvars.ContextVar(
    "_current_timer", default=None
)


class TurnTimer:
    """Context manager that measures wall time for a full Discord turn.

    Phases are recorded as deltas from the previous mark (or start).
    Final log contains total + per-phase breakdown in activity_log.
    """

    def __init__(
        self,
        *,
        turn_id: str,
        channel_id: str | None = None,
        person_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.turn_id = turn_id
        self.channel_id = channel_id
        self.person_id = person_id
        self.session_id = session_id

        self._start = time.monotonic()
        self._last = self._start
        self.phases: dict[str, int] = {}  # name -> ms since previous mark

    async def __aenter__(self) -> "TurnTimer":
        self._token = _current_timer.set(self)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        total_ms = int((time.monotonic() - self._start) * 1000)
        try:
            import db_writes
            meta = {
                "session_id": self.session_id,
                "phases": self.phases,
            }
            log_coro = db_writes.routed(
                "log_turn_timing",
                turn_id=self.turn_id,
                total_ms=total_ms,
                setup_ms=self.phases.get("setup", 0),
                context_ms=self.phases.get("context", 0),
                llm_ms=self.phases.get("llm", 0),
                tools_ms=self.phases.get("tools", 0),
                send_ms=self.phases.get("send", 0),
                channel_id=self.channel_id,
                person_id=self.person_id,
                metadata=meta,
            )
            self._log_task = fire_and_forget(log_coro)
        except Exception:
            log.debug("TurnTimer: failed to log turn_timing (non-fatal)", exc_info=True)
            self._log_task = None
        finally:
            try:
                _current_timer.reset(self._token)
            except Exception:
                pass

    @classmethod
    def current(cls) -> "TurnTimer | None":
        """Return the TurnTimer active in this context (if any). Used by deep
        instrumentation (build_context legs, tool execution) to record phases
        and to obtain a stable turn_id for correlation in activity_log rows.
        """
        return _current_timer.get()

    def mark(self, name: str) -> None:
        """Record elapsed ms since last mark (or start) under this phase name.

        Sequential deltas. Inner calls (e.g. context) will update _last so outer
        subsequent marks capture the remaining time correctly.
        """
        now = time.monotonic()
        delta = int((now - self._last) * 1000)
        self.phases[name] = delta
        self._last = now

    def record(self, name: str, ms: int) -> None:
        """Record a named duration (e.g. sub-phase) without disturbing the
        sequential mark chain. Useful for context/tools inside larger 'llm' window.
        """
        self.phases[name] = int(ms or 0)

    def advance(self) -> None:
        """Advance the last time mark without adding a new named delta.
        Used after recording a sub-phase duration so subsequent marks capture
        time after the sub-phase.
        """
        self._last = time.monotonic()

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)
