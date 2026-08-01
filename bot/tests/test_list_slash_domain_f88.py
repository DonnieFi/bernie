"""family-bot-f88: list_slash_commands domain is search, not notify."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestListSlashDomainF88(unittest.TestCase):
    def test_registry_domain_is_search(self):
        from tools import load_all_domains, get_registry

        load_all_domains()
        reg = get_registry()
        self.assertIn("list_slash_commands", reg)
        entry = reg["list_slash_commands"]
        domain = entry.get("domain") if isinstance(entry, dict) else getattr(entry, "domain", None)
        self.assertEqual(domain, "search")

    def test_source_not_notify(self):
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / "tools" / "admin.py"
        text = src.read_text(encoding="utf-8")
        i = text.index('name="list_slash_commands"')
        chunk = text[i : i + 450]
        self.assertIn('domain="search"', chunk)
        self.assertNotIn('domain="notify"', chunk)


if __name__ == "__main__":
    unittest.main()
