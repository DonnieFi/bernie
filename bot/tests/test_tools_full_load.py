"""Smoke test: all domain modules import cleanly and register their tools."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestToolsFullLoad(unittest.TestCase):
    def test_tools_transit_syntax(self):
        import ast

        path = os.path.join(os.path.dirname(__file__), "..", "tools", "transit.py")
        with open(path, encoding="utf-8") as f:
            ast.parse(f.read(), filename=path)

    def test_all_domains_load(self):
        from tools import get_registry, load_all_domains

        load_all_domains()
        reg = get_registry()
        self.assertGreaterEqual(
            len(reg),
            50,
            f"Expected 50+ tools registered, got {len(reg)}: {sorted(reg.keys())}",
        )

    def test_required_tools_present(self):
        from tools import get_registry, load_all_domains

        load_all_domains()
        reg = get_registry()
        expected = {
            # calendar
            "get_todays_events", "get_week_events", "get_month_events", "create_event",
            "get_historical_events", "get_events_range", "get_rsvps",
            "get_school_schedule", "get_homework", "get_highlights",
            # home
            "control_device", "set_light", "trigger_automation",
            "get_home_state", "get_home_health", "get_network_devices",
            "get_vehicle_status", "get_sleep_summary",
            # weather, search, presence, media
            "get_current_weather",
            "fetch_url", "web_search",
            "who_is_home", "get_person_location", "get_battery",
            "play_media", "media_control",
            # tasks + automations
            "create_task", "list_tasks", "complete_task", "approve_task",
            "update_task", "delete_task", "snooze_task", "decline_task",
            "create_automation", "list_automations", "toggle_automation", "delete_automation",
            # meals
            "get_meals", "set_meal", "delete_meal", "search_food_ideas",
            "add_grocery_item", "remove_grocery_item", "get_grocery_list",
            # admin
            "trigger_system_audit",
            "litellm_list_models", "litellm_add_model", "litellm_remove_model", "litellm_switch_model",
            "reset_web_pin", "reload_config",
            "get_system_health", "get_container_logs",
            "get_langfuse_traces", "get_langfuse_metrics",
            # identity, memory, notify, cognitive
            "get_identity_info", "resolve_entity", "get_unresolved_entities",
            "read_family_context", "update_family_context",
            "read_person_context", "update_person_context",
            "notify_family_member", "send_email",
            "get_recent_email_signals", "read_email_message",
            "ask_ollama", "defer_response", "request_research",
            # Task 8 stragglers
            "get_camera_snapshot", "get_garbage_schedule", "get_oura_sleep",
            # kanban
            "kanban_show", "kanban_create", "kanban_heartbeat", "kanban_comment",
            "kanban_complete", "kanban_block", "kanban_link",
            # network
            "get_network_speedtest", "get_network_status",
            # transit, modes, frigate (modes domain added to load_all_domains in Phase 29)
            "get_route_buses", "get_bus_proximity", "track_vehicle",
            "switch_mode",
            "frigate_set_camera", "frigate_set_hours",
        }
        missing = expected - reg.keys()
        self.assertFalse(missing, f"Missing tools: {sorted(missing)}")

    def test_write_tools_flagged(self):
        """All state-mutating tools must have is_write=True so ToolGateway blocks them in shadow runs."""
        from tools import get_registry, load_all_domains

        load_all_domains()
        reg = get_registry()
        must_be_write = {
            "create_event", "control_device", "set_light", "trigger_automation",
            "play_media", "media_control",
            "create_task", "complete_task", "approve_task", "update_task",
            "delete_task", "snooze_task", "decline_task",
            "create_automation", "toggle_automation", "delete_automation",
            "set_meal", "delete_meal", "add_grocery_item", "remove_grocery_item",
            "update_family_context", "update_person_context",
            "notify_family_member",
            "defer_response", "request_research",
            "litellm_add_model", "litellm_remove_model", "litellm_switch_model",
            "reset_web_pin", "trigger_system_audit", "reload_config",
            "send_email",
        }
        wrong = [name for name in must_be_write if not reg[name]["is_write"]]
        self.assertFalse(wrong, f"Tools missing is_write=True: {wrong}")

    def test_read_tools_not_write(self):
        """Read-only tools should NOT be flagged is_write — they execute in shadow runs."""
        from tools import get_registry, load_all_domains

        load_all_domains()
        reg = get_registry()
        must_not_write = {
            "get_todays_events", "get_week_events", "get_month_events",
            "get_historical_events", "get_events_range", "get_rsvps",
            "get_school_schedule", "get_homework", "get_highlights",
            "get_home_state", "get_home_health", "get_network_devices",
            "get_vehicle_status", "get_sleep_summary",
            "get_current_weather",
            "fetch_url", "web_search",
            "who_is_home", "get_person_location", "get_battery",
            "list_tasks", "list_automations",
            "get_meals", "search_food_ideas", "get_grocery_list",
            "read_family_context", "read_person_context",
            "get_recent_email_signals", "read_email_message",
            "get_identity_info", "resolve_entity", "get_unresolved_entities",
            "litellm_list_models", "get_system_health", "get_container_logs",
            "get_network_speedtest", "get_network_status",
            "get_langfuse_traces", "get_langfuse_metrics",
            "ask_ollama",
            "get_camera_snapshot", "get_garbage_schedule", "get_oura_sleep",
        }
        wrong = [name for name in must_not_write if reg[name]["is_write"]]
        self.assertFalse(wrong, f"Read tools wrongly flagged is_write: {wrong}")

    def test_all_tools_have_tier_metadata(self):
        from tools import get_registry, load_all_domains

        load_all_domains()
        reg = get_registry()
        missing = [n for n, e in reg.items() if not isinstance(e.get("tier"), int)]
        self.assertFalse(missing, f"Tools missing int tier: {missing}")
