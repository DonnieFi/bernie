# bot/task_status.py
"""Pure mappings between the legacy chore model and the unified task model. No I/O."""

UNIFIED_STATUSES = ("triage", "todo", "ready", "running", "blocked", "done", "archived")
ACTIVE_LANES = ("triage", "todo", "ready", "running", "blocked")


def to_unified_status(legacy_status: str, in_progress: bool) -> str:
    if legacy_status in ("done", "approved"):
        return "done"
    return "running" if in_progress else "todo"


def to_legacy_status(unified_status: str) -> tuple[str, bool]:
    """Unified status -> (legacy_status, in_progress). NOTE: done-vs-approved is decided by the
    caller using approved_at; this returns ('done', False) for both done and archived."""
    if unified_status == "running":
        return ("pending", True)
    if unified_status in ("done", "archived"):
        return ("done", False)
    return ("pending", False)


def due_to_horizon(due_at: str | None) -> str:
    if not due_at:
        return "someday"
    return due_at[:7]


def legacy_status_to_unified(legacy_filter: str) -> tuple[str, ...]:
    """Map a list-filter ('all'|'pending'|'done'|'approved') to the unified statuses to filter on.
    '()' means no status filter (caller still excludes 'archived')."""
    if legacy_filter == "pending":
        return ACTIVE_LANES
    if legacy_filter in ("done", "approved"):
        return ("done",)
    return ()
