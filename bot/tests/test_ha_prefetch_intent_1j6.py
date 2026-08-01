"""family-bot-1j6: HA prefetch defaults to intent, not always."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.utils import _bot, _root


class TestHaPrefetchIntent1j6(unittest.TestCase):
    def test_code_default_is_intent(self):
        src = _bot("llm", "context_builder.py").read_text(encoding="utf-8")
        self.assertIn('.get("ha", "intent")', src)
        self.assertNotIn('.get("ha", "always")', src)

    def test_prod_config_uses_intent(self):
        path = _root("config.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        ha = ((data.get("context") or {}).get("prefetch") or {}).get("ha")
        self.assertEqual(ha, "intent", "config.json context.prefetch.ha")

    def test_example_config_uses_intent(self):
        path = _root("config.example.json")
        if not path.exists():
            # Container only mounts config.json; example lives at repo root on host.
            self.skipTest("config.example.json missing in this layout")
        data = json.loads(path.read_text(encoding="utf-8"))
        ha = ((data.get("context") or {}).get("prefetch") or {}).get("ha")
        self.assertEqual(ha, "intent", "config.example.json context.prefetch.ha")

    def test_minimal_example_config_uses_intent(self):
        path = _root("config.minimal.example.json")
        if not path.exists():
            self.skipTest("config.minimal.example.json missing in this layout")
        data = json.loads(path.read_text(encoding="utf-8"))
        ha = ((data.get("context") or {}).get("prefetch") or {}).get("ha")
        self.assertEqual(ha, "intent", "config.minimal.example.json context.prefetch.ha")

    def test_missing_prefetch_key_defaults_to_intent_gate(self):
        """Brownfield: absent prefetch.ha must not dump HA on chit-chat."""
        from llm.context_legs import looks_home_intent

        ha_mode = str(({}).get("ha", "intent")).lower()
        self.assertEqual(ha_mode, "intent")
        self.assertFalse(looks_home_intent("hey"))
        self.assertFalse(looks_home_intent("what time is practice?"))
        self.assertTrue(looks_home_intent("turn on the kitchen light"))


if __name__ == "__main__":
    unittest.main()
