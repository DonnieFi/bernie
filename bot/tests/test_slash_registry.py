"""Tests for the authoritative slash command registry (AST extracted)."""
import unittest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from slash_registry import get_all_slash_commands, list_slash_command_names
from tools import load_all_domains, get_registry

class TestSlashRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_all_domains()
        cls.registry = get_registry()
        cls.cmds = get_all_slash_commands()
        cls.names = [c['name'] for c in cls.cmds]

    def test_no_hand_maintained_dupe(self):
        # The admin list_slash handler must not contain a giant static commands = [...]
        # (we import instead). This is a source hygiene check.
        import inspect
        from tools import admin as admin_mod
        src = inspect.getsource(admin_mod.handle_list_slash_commands)
        # If we still see the old giant literal, it would have many {"name":
        self.assertLess(src.count('{"name":'), 5, "list_slash should delegate to slash_registry, not embed large static list")

    def test_config_commands_present(self):
        self.assertIn('config_summary', self.names)
        self.assertIn('config_reminders', self.names)

    def test_bus_group(self):
        self.assertTrue(any(n.startswith('bus ') or n == 'bus' for n in self.names))

    def test_no_shadow_mode(self):
        self.assertNotIn('shadow_mode', self.names)

    def test_list_slash_commands_registered(self):
        self.assertIn("list_slash_commands", self.registry)

    def test_count_reasonable(self):
        # Previously ~43-48; AST should give similar or better.
        self.assertGreaterEqual(len(self.names), 40)
