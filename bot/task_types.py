"""Pure type→assignee gating. No I/O. Config-driven so adding a member/agent is a config edit."""
import fnmatch

def validate_assignment(task_type: str, assignee: str | None, config: dict) -> bool:
    """True if `assignee` may hold a task of `task_type` per config['task_types'].
    - `system` is never user-assignable/creatable.
    - assignee=None (open to claim) is allowed for any real type.
    - unknown type → rejected."""
    if task_type == "system":
        return False
    patterns = (config.get("task_types") or {}).get(task_type)
    if patterns is None:
        return False
    if not assignee:
        return True
    norm = assignee if ":" in assignee else f"person:{assignee}"
    return any(fnmatch.fnmatch(norm, p) for p in patterns)
