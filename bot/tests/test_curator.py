"""Pure unit tests for weekly Curator prune filters (family-bot-5hy.8)."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jobs.curator import format_curator_report, is_prune_candidate


class TestIsPruneCandidate(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)

    def test_high_confidence_kept(self):
        old = (self.now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertFalse(is_prune_candidate(0.5, old, now=self.now))
        self.assertFalse(is_prune_candidate(0.3, old, now=self.now))  # not strictly <

    def test_low_conf_fresh_kept(self):
        recent = (self.now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertFalse(is_prune_candidate(0.1, recent, now=self.now))

    def test_low_conf_stale_pruned(self):
        old = (self.now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertTrue(is_prune_candidate(0.2, old, now=self.now))
        self.assertTrue(is_prune_candidate(0.0, old, now=self.now))

    def test_missing_timestamp_kept(self):
        self.assertFalse(is_prune_candidate(0.1, None, now=self.now))
        self.assertFalse(is_prune_candidate(0.1, "", now=self.now))

    def test_iso_without_z(self):
        old = (self.now - timedelta(days=40)).isoformat()
        self.assertTrue(is_prune_candidate(0.1, old, now=self.now))


class TestFormatCuratorReport(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertIsNone(format_curator_report([], []))

    def test_includes_counts(self):
        msg = format_curator_report(
            [{"person_id": "child1", "name": "piano", "confidence": 0.1}],
            [{"person_id": "dad", "observation": "likes coffee", "confidence": 0.2}],
        )
        self.assertIsNotNone(msg)
        self.assertIn("1 routine", msg)
        self.assertIn("1 observation", msg)
        self.assertIn("piano", msg)
        self.assertIn("coffee", msg)


if __name__ == "__main__":
    unittest.main()
