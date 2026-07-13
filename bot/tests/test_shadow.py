"""Tests for eval.shadow queue shedding helpers."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.shadow import _shed_shadow_first


class TestShadowQueueShed(unittest.TestCase):
    def test_explicit_shed_skips_policy_resolution(self):
        self.assertTrue(_shed_shadow_first({}, True))
        self.assertFalse(_shed_shadow_first({}, False))

    def test_falls_back_to_capture_nested_key(self):
        config = {
            "eval": {
                "capture": {"enabled": True, "shed_on_backpressure": False},
            },
        }
        self.assertFalse(_shed_shadow_first(config, None))
