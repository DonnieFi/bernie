"""family-bot-9xg: tighter multistep patterns."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from llm.intent import looks_multistep


class TestSmolIntent9xg(unittest.TestCase):
    def test_still_catches_real_multistep(self):
        self.assertTrue(looks_multistep("plan dinner around everyone's schedule", {}))
        self.assertTrue(looks_multistep("check each kid's homework and then remind them", {}))
        self.assertTrue(looks_multistep("Compare Oura and Garmin", {}))
        self.assertTrue(looks_multistep("Is Child1 home? And what's for dinner?", {}))

    def test_false_positives_fixed(self):
        self.assertFalse(looks_multistep("what's the plan?", {}))
        self.assertFalse(looks_multistep("we can each have ice cream", {}))  # each without kid/of
        self.assertFalse(looks_multistep("huh??", {}))
        self.assertFalse(looks_multistep("is anyone home?", {}))


if __name__ == "__main__":
    unittest.main()
