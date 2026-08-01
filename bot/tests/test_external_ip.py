"""Pure unit tests for daily external-IP change detection (family-bot-5hy.3)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jobs.external_ip import ip_change_alert


class TestIpChangeAlert(unittest.TestCase):
    def test_first_observation_no_alert(self):
        self.assertIsNone(ip_change_alert(None, "1.2.3.4"))
        self.assertIsNone(ip_change_alert("", "1.2.3.4"))
        self.assertIsNone(ip_change_alert("   ", "1.2.3.4"))

    def test_unchanged_no_alert(self):
        self.assertIsNone(ip_change_alert("1.2.3.4", "1.2.3.4"))
        self.assertIsNone(ip_change_alert(" 1.2.3.4 ", "1.2.3.4"))

    def test_change_alerts(self):
        msg = ip_change_alert("1.2.3.4", "5.6.7.8")
        self.assertIsNotNone(msg)
        self.assertIn("1.2.3.4", msg)
        self.assertIn("5.6.7.8", msg)
        self.assertIn("Public IP changed", msg)

    def test_empty_current_no_alert(self):
        self.assertIsNone(ip_change_alert("1.2.3.4", None))
        self.assertIsNone(ip_change_alert("1.2.3.4", ""))
        self.assertIsNone(ip_change_alert(None, None))


if __name__ == "__main__":
    unittest.main()
