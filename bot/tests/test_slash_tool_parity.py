"""Discord slash command ↔ @tool parity (container-safe).

Runnable on bernie-host:
  docker compose -f /opt/family-bot/docker-compose.yml exec -T bernie-discord \
    bash -lc 'cd /app && python -m unittest tests.test_slash_tool_parity -v'
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slash_registry import get_all_slash_commands
from tools import get_registry, load_all_domains

# Deliberately excluded from NL parity (CLAUDE.md).
SLASH_EXEMPT = frozenset({"shadow_mode"})

# Parent /bus group row — subcommands carry the real mappings.
SLASH_GROUP_ONLY = frozenset({"bus"})

# Guidance or composite slashes whose observable outcome is another @tool.
SLASH_TOOL_EQUIV: dict[str, str] = {
    "addevent": "create_event",
    "setreminder": "create_event",
    "bus help": "list_slash_commands",
    "today": "get_todays_events",
    "summary": "get_highlights",
    "week": "get_week_events",
    "weather": "get_current_weather",
    "flight": "get_flight_status",
    "garbage": "get_garbage_schedule",
    "school": "get_school_schedule",
    # /school_schedule toggles config (on/off), not a read — mirrors set_show_school_in_daily_summary @tool
    "school_schedule": "set_show_school_in_daily_summary",
    "homework": "get_homework",
    "rsvps": "get_rsvps",
    "task_add": "create_task",
    "task_list": "list_tasks",
    "task_done": "complete_task",
    "task_snooze": "snooze_task",
    "task_no": "decline_task",
    "task_approve": "approve_task",
    "automation_add": "create_automation",
    "automation_list": "list_automations",
    "automation_toggle": "toggle_automation",
    "automation_delete": "delete_automation",
    "speedtest": "get_network_speedtest",
    "snap": "get_camera_snapshot",
    "model": "litellm_switch_model",
    "model-add": "litellm_add_model",
    "model-remove": "litellm_remove_model",
    "reload": "reload_config",
    "audit": "trigger_system_audit",
    "network": "get_network_status",
    "email": "send_email",
    "frigate_camera": "frigate_set_camera",
    "frigate_mode": "frigate_set_mode",
    "mode": "switch_mode",
    "eval_mode": "set_eval_mode",
    "nightly_eval": "set_nightly_eval_mode",
    "harness_mode": "set_harness_mode",
    "eval_scoring": "set_eval_scoring",
    "hitl_mode": "set_hitl_mode",
    "worker_model": "set_worker_model",
    "eval_status": "get_eval_status",
    "config_summary": "set_config_summary",
    "config_reminders": "set_config_reminders",
    "reminders": "set_reminders",
    "dm": "set_dm_mode",
    "settings": "get_settings",
    "temps": "get_temperatures",
    "ha_entities": "list_ha_entities",
    "bus near": "get_bus_proximity",
    "bus route": "get_route_buses",
    "bus stop": "stop_bus_tracking",
    "bus track": "track_vehicle",
}


def resolve_slash_tool(slash_name: str) -> str:
    """Map a slash display name to the @tool that provides NL parity."""
    if slash_name in SLASH_TOOL_EQUIV:
        return SLASH_TOOL_EQUIV[slash_name]
    return slash_name.replace("-", "_")


class TestSlashToolParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_all_domains()
        cls.registry = get_registry()
        cls.tool_names = set(cls.registry.keys())
        cls.slash_names = [
            c["name"]
            for c in get_all_slash_commands()
            if c["name"] not in SLASH_EXEMPT
        ]

    def test_registry_has_list_slash_commands(self):
        self.assertIn("list_slash_commands", self.tool_names)

    def test_slash_registry_count(self):
        self.assertGreaterEqual(len(self.slash_names), 40)

    def test_every_slash_has_tool_or_documented_equivalent(self):
        missing: list[str] = []
        for slash in self.slash_names:
            if slash in SLASH_GROUP_ONLY:
                continue
            tool = resolve_slash_tool(slash)
            if tool not in self.tool_names:
                missing.append(f"/{slash} → {tool} (not in registry)")
        self.assertFalse(
            missing,
            "Slash commands without @tool parity:\n  "
            + "\n  ".join(sorted(missing)),
        )

    def test_documented_equivalents_are_registered(self):
        for slash, tool in SLASH_TOOL_EQUIV.items():
            with self.subTest(slash=slash, tool=tool):
                self.assertIn(tool, self.tool_names)
