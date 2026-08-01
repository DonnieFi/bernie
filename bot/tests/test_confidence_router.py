import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from confidence_router import route_deliverable
from typed_outputs import DeliverableMeta


class TestConfidenceRouter(unittest.TestCase):
    def test_low_confidence_ignored(self):
        meta = DeliverableMeta(confidence=0.2)
        self.assertEqual(route_deliverable(meta), "ignore")

    def test_high_interrupt_routes_interrupt(self):
        meta = DeliverableMeta(confidence=0.9, urgency="high", impact="high", interrupt=True)
        self.assertEqual(route_deliverable(meta), "interrupt")

    def test_medium_confidence_suggest(self):
        meta = DeliverableMeta(confidence=0.7, impact="medium")
        self.assertEqual(route_deliverable(meta), "suggest")

    def test_remember_band(self):
        meta = DeliverableMeta(confidence=0.45, impact="low")
        self.assertEqual(route_deliverable(meta), "remember")


if __name__ == "__main__":
    unittest.main()
