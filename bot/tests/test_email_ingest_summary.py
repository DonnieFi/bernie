"""Phase 34 — EmailIngestSummary typed output validation."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pydantic import ValidationError

from typed_outputs import EmailIngestSummary


class TestEmailIngestSummary(unittest.TestCase):
    def test_valid_summary(self):
        m = EmailIngestSummary(summary="School field trip Friday", topics=["school"], confidence=0.9)
        self.assertEqual(m.summary, "School field trip Friday")

    def test_summary_max_length(self):
        with self.assertRaises(ValidationError):
            EmailIngestSummary(summary="x" * 301, confidence=0.5)

    def test_confidence_bounds(self):
        with self.assertRaises(ValidationError):
            EmailIngestSummary(summary="ok", confidence=1.5)