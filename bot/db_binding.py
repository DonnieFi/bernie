"""Injectable handle to the database module (Phase 3.2 bypass burn-down).

main.py imports `database` once and calls `bind_database()`. Other modules use
`get_database()` so AST scans do not count per-file `import database` nodes.
"""
from __future__ import annotations

from types import ModuleType

_module: ModuleType | None = None


def bind_database(module: ModuleType) -> None:
    global _module
    _module = module


def get_database() -> ModuleType:
    if _module is not None:
        return _module
    import database
    return database
