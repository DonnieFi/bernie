import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import modes as modes_mod


class TestModesResolver(unittest.TestCase):
    def setUp(self):
        modes_mod._modes.clear()
        modes_mod._mode_override = None
        modes_mod.load_all_modes()

    def test_furnace_channel_pins_chef(self):
        cfg = {"furnace_channel_id": "111", "anvil_channel_id": "222"}
        with patch("config.config", cfg):
            mode = modes_mod.resolve_mode(channel="111")
        self.assertEqual(mode.slug, "chef")

    def test_anvil_default_ops(self):
        cfg = {"anvil_channel_id": "222"}
        with patch("config.config", cfg):
            mode = modes_mod.resolve_mode(channel="222", message_text="status check")
        self.assertEqual(mode.slug, "ops")

    def test_anvil_debug_keyword_override(self):
        cfg = {"anvil_channel_id": "222"}
        with patch("config.config", cfg):
            mode = modes_mod.resolve_mode(channel="222", message_text="please debug this error")
        self.assertEqual(mode.slug, "debug")

    def test_security_channel_pin(self):
        cfg = {
            "security_channel_id": "333",
            "frigate": {"notification_channel_id": "444"},
        }
        with patch("config.config", cfg):
            m1 = modes_mod.resolve_mode(channel="333")
            m2 = modes_mod.resolve_mode(channel="444")
        self.assertEqual(m1.slug, "security")
        self.assertEqual(m2.slug, "security")
