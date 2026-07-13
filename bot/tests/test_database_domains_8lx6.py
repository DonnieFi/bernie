"""family-bot-8lx.6: database misc second domain pass re-exports."""
from __future__ import annotations

import unittest


class TestDatabaseDomainReexports(unittest.TestCase):
    def test_split_domains_reexported_from_package(self):
        import database

        for name in (
            "get_meals",
            "set_meal",
            "get_groceries",
            "add_grocery",
            "get_presence",
            "update_presence",
            "get_last_home_signal",
            "log_notification",
            "list_pending_notifications",
            "get_notification_log",
            "get_weather_location",
            "set_weather_snapshot",
            "store_draft",
            "get_draft",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(database, name), f"missing database.{name}")
                self.assertTrue(callable(getattr(database, name)))


if __name__ == "__main__":
    unittest.main()
