"""Tool registry — domain handlers register here via @tool decorator.

Each domain file (calendar, home, weather, …) imports `tool` from this module
and decorates async handler functions. The decorator stores the handler plus
its metadata (description, JSON schema, role gate, write flag) in `_registry`.

`ToolGateway.execute()` is the single dispatcher — every executor goes through
it for RBAC, schema validation, shadow-write blocking, retry, and span emission.
"""
from __future__ import annotations

import sys
from typing import Any, Callable, Literal

from constants import ROLE_ADMIN, ROLE_ALL, ROLE_BERNIE, ROLE_PARENTS, ROLE_SYSTEM

# Re-export for `from tools import ROLE_ALL` call sites.
__all__ = [
    "ROLE_ALL", "ROLE_PARENTS", "ROLE_ADMIN", "ROLE_BERNIE", "ROLE_SYSTEM",
    "tool", "get_registry", "load_all_domains", "effective_tier",
]

_registry: dict[str, dict] = {}
_domains_loaded = False


def _coerce_tier(tier: Literal[1, 2, 3] | int | str | None) -> int:
    """Coerce tier parameter to an integer (1, 2, or 3)."""
    if tier is None:
        return 3
    try:
        coerced = int(tier)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid tier value: {tier}. Must be 1, 2, or 3.")
    if coerced not in (1, 2, 3):
        raise ValueError(f"Tier must be 1, 2, or 3, got: {coerced}")
    return coerced


def effective_tier(entry: dict) -> int:
    """Return the tier for a tool registry entry, defaulting to 3 if missing."""
    tier = entry.get("tier")
    if tier is None:
        return 3
    try:
        return _coerce_tier(tier)
    except ValueError:
        return 3


def tool(
    *,
    name: str,
    description: str,
    input_schema: dict,
    role_required: str = ROLE_ALL,
    is_write: bool = False,
    domain: str | None = None,
    tier: Literal[1, 2, 3] | int | str | None = None,
) -> Callable:
    """Register an async tool handler in the global registry.

    Args:
        name: Tool identifier used by the LLM and gateway dispatch.
        description: Human-readable description sent to the model.
        input_schema: JSON Schema for the args dict — validated by ToolGateway.
        role_required: Minimum role to call this tool (family/parents/admin).
        is_write: True if the tool mutates state; ToolGateway blocks during shadow runs.
        domain: Optional grouping key — ToolGateway uses this to filter (e.g.
            calendar tools when no calendar service is available). When omitted,
            falls back to the handler's package leaf name (e.g. `tools.calendar`
            → `calendar`).
        tier: Safe execution tier (1, 2, or 3). 1 = read-only/idempotent,
            2 = recoverable write, 3 = danger/destructive write.
    """
    coerced_tier = _coerce_tier(tier)

    def decorator(fn: Callable) -> Callable:
        resolved_domain = domain
        if resolved_domain is None:
            mod = getattr(fn, "__module__", "") or ""
            resolved_domain = mod.rsplit(".", 1)[-1] if mod else ""
        _registry[name] = {
            "fn": fn,
            "name": name,
            "description": description,
            "input_schema": input_schema,
            "role_required": role_required,
            "is_write": is_write,
            "domain": resolved_domain,
            "tier": coerced_tier,
        }
        return fn

    return decorator


def get_registry() -> dict[str, dict]:
    return _registry


_DOMAIN_MODULES = (
    "calendar",
    "home",
    "media",
    "network",
    "presence",
    "weather",
    "search",
    "tasks",
    "meals",
    "admin",
    "notify",
    "email",
    "identity",
    "memory",
    "cognitive",
    "kanban",
    "snapshots",
    "transit",
    "flights",
    "modes",
    "discovery",
)


def load_all_domains() -> None:
    """Import all domain modules to trigger @tool registration.

    Intentionally explicit — auto-discovery via pkgutil.walk_packages would
    silently register scratch files or half-finished modules. When adding a
    new domain file, add it to ``_DOMAIN_MODULES``.

    Idempotent. Prefer a single call at process startup (``main.py`` / ``bot.py``)
    or ``get_tool_gateway()``. ``ToolGateway.execute`` only calls this on an
    empty registry (cold start) — not on every tool call (family-bot-1ov.4).
    """
    global _domains_loaded
    if _domains_loaded and _registry:
        return

    import importlib
    import os

    # family-bot-5s6: importlib.reload only in tests (cleared registry re-registers @tool).
    # Production never reload — double side-effects / broken module globals.
    _in_tests = (
        os.environ.get("BERNIE_TESTING") == "1"
        or "unittest" in sys.modules
    )
    reload_mode = _domains_loaded and not _registry and _in_tests
    for mod in _DOMAIN_MODULES:
        full = f"tools.{mod}"
        if reload_mode and full in sys.modules:
            importlib.reload(sys.modules[full])
        else:
            importlib.import_module(full)
    _domains_loaded = True
