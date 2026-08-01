"""Discord slash command registration (peeled from bot.py).

family-bot-1od: each domain module is a real Python file with ``register(tree, m)``
and explicit bot bindings — no ``exec`` of ``_*_src.py``.

Usage (from bot.on_ready, before tree.sync):
    from slash import register_all
    register_all(tree, sys.modules[__name__])
"""
from __future__ import annotations

from typing import Any

from discord import app_commands

from . import admin_cmds, family_cmds, flight_cmds, home_cmds, prefs_cmds, school_cmds, tasks_cmds

_MODULES = (
    family_cmds,
    prefs_cmds,
    tasks_cmds,
    school_cmds,
    home_cmds,
    flight_cmds,
    admin_cmds,
)


def register_all(tree: app_commands.CommandTree, m: Any) -> None:
    """Register all peeled slash commands onto *tree*.

    *m* is the bot module (sys.modules["bot"]) providing cal, helpers, services.
    """
    for mod in _MODULES:
        mod.register(tree, m)
