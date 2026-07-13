"""Phase 34 — Gmail address normalization."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from email_service import family_email_set, normalize_email


class TestEmailNormalize(unittest.TestCase):
    def test_gmail_dots_and_plus_alias(self):
        self.assertEqual(
            normalize_email("S.Mithy+news@gmail.com"),
            normalize_email("smithy@gmail.com"),
        )

    def test_family_set_uses_canonical_gmail(self):
        cfg = {
            "family_members": {
                "Dad": {"email": "smithy@gmail.com", "role": "admin"},
            }
        }
        family = family_email_set(cfg)
        self.assertIn(normalize_email("s.mithy+alias@gmail.com"), family)