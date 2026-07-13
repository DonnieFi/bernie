"""Pure helpers for routing notifications — no I/O, no Discord, unit-testable."""


def blocked_ping_recipient(task: dict) -> str | None:
    """Return the canonical id to DM when a task becomes blocked.

    Prefers ``assigned_by`` when it names a person (anything that is not
    empty and does not start with ``agent:``).  Returns ``None`` when the
    assigner is an agent or absent — callers should route to the ``#anvil``
    admin channel in that case.

    Convention: persons use bare canonical ids (``"mom"``, ``"dad"``)
    or the legacy ``"person:<name>"`` prefix.  Agents always start with
    ``"agent:"``.
    """
    by = task.get("assigned_by") or ""
    return by if (by and not by.startswith("agent:")) else None
