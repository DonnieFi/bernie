"""family-bot-8lx.7: auto-export surface from database package."""
from __future__ import annotations

import unittest


class TestDatabaseAutoExports(unittest.TestCase):
    def test_known_private_helpers_exported(self):
        import database as db

        for name in ("_db_conn", "_db_read", "_pkg", "_resolve_db_path"):
            self.assertTrue(hasattr(db, name), f"expected private re-export {name}")

    def test_unknown_private_not_exported(self):
        import database as db

        # Domain-local private that must not leak via package auto-export
        self.assertFalse(hasattr(db, "_FALLBACK_RATES"))
        # Not on the allowlist and not a public symbol
        self.assertFalse(hasattr(db, "_build_name_map"))


if __name__ == "__main__":
    unittest.main()
