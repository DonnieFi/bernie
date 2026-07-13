import unittest
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo

from eval.policy import resolve_eval_policy, harness_active


class TestEvalPolicy(unittest.TestCase):
    def test_precedence_neither(self):
        # 3. Neither (defaults)
        config = {}
        policy = resolve_eval_policy(config)
        
        self.assertFalse(policy.capture_enabled)
        self.assertEqual(policy.defer_s, 2)
        self.assertTrue(policy.shed_on_backpressure)
        
        self.assertFalse(policy.harness_enabled)
        self.assertTrue(policy.block_peak_hours)
        self.assertEqual(policy.peak_start_hour, 15)
        self.assertEqual(policy.peak_end_hour, 21)
        
        self.assertFalse(policy.nightly_enabled)
        self.assertTrue(policy.score_pairs)
        self.assertTrue(policy.score_triplets)
        self.assertFalse(policy.hitl)
        self.assertTrue(policy.ungrounded_audit)

    def test_precedence_legacy_only(self):
        # 2. Legacy only
        config = {
            "eval": {
                "enabled": True,  # legacy flat key
            },
            "executor": {
                "shadow_defer_s": 5,
                "llm_queue_shed_shadow_first": False,
                "shadow_harness_enabled": True
            }
        }
        policy = resolve_eval_policy(config)
        
        self.assertTrue(policy.capture_enabled) # falls back to eval.enabled
        self.assertEqual(policy.defer_s, 5)
        self.assertFalse(policy.shed_on_backpressure)
        self.assertTrue(policy.harness_enabled)
        self.assertTrue(policy.nightly_enabled) # falls back to eval.enabled

    def test_precedence_nested_and_legacy(self):
        # 1. Both nested + legacy present (nested wins)
        config = {
            "eval": {
                "enabled": False, # legacy flat key
                "capture": {
                    "enabled": True,
                    "defer_s": 10,
                    "shed_on_backpressure": True
                },
                "harness": {
                    "enabled": False,
                    "block_peak_hours": False
                },
                "nightly": {
                    "enabled": True,
                    "score_pairs": False
                }
            },
            "executor": {
                "shadow_defer_s": 5,
                "llm_queue_shed_shadow_first": False,
                "shadow_harness_enabled": True
            }
        }
        policy = resolve_eval_policy(config)
        
        # Nested wins
        self.assertTrue(policy.capture_enabled)
        self.assertEqual(policy.defer_s, 10)
        self.assertTrue(policy.shed_on_backpressure)
        self.assertFalse(policy.harness_enabled)
        self.assertFalse(policy.block_peak_hours)
        self.assertTrue(policy.nightly_enabled)
        self.assertFalse(policy.score_pairs)

    @patch('eval.policy.datetime')
    def test_peak_hours_july_dst(self, mock_dt):
        # DST: Mock specific date in July (ADT -0300)
        # America/Halifax in July is ADT. 
        # 16:00 local time should be blocked.
        tz = ZoneInfo("America/Halifax")
        # Let's mock datetime.now to return a datetime with timezone
        dt = datetime(2026, 7, 15, 16, 0, 0, tzinfo=tz)
        mock_dt.now.return_value = dt
        
        config = {
            "eval": {
                "harness": {
                    "enabled": True,
                    "block_peak_hours": True,
                    "peak_start_hour": 15,
                    "peak_end_hour": 21
                }
            },
            "timezone": "America/Halifax"
        }
        policy = resolve_eval_policy(config)
        
        # Active would be False because it's peak hours (16 is between 15 and 21)
        self.assertFalse(harness_active(policy))
        
        # If outside peak hours
        dt_outside = datetime(2026, 7, 15, 14, 0, 0, tzinfo=tz)
        mock_dt.now.return_value = dt_outside
        self.assertTrue(harness_active(policy))

    @patch('eval.policy.datetime')
    def test_peak_hours_january_ast(self, mock_dt):
        # Standard time: Mock specific date in January (AST -0400)
        tz = ZoneInfo("America/Halifax")
        dt = datetime(2026, 1, 15, 16, 0, 0, tzinfo=tz)
        mock_dt.now.return_value = dt
        
        config = {
            "eval": {
                "harness": {
                    "enabled": True,
                    "block_peak_hours": True
                }
            },
            "timezone": "America/Halifax"
        }
        policy = resolve_eval_policy(config)
        self.assertFalse(harness_active(policy))

    def test_invalid_timezone(self):
        # Invalid timezone gracefully skips peak check
        config = {
            "eval": {
                "harness": {
                    "enabled": True,
                    "block_peak_hours": True
                }
            },
            "timezone": "Invalid/Timezone"
        }
        policy = resolve_eval_policy(config)
        # It shouldn't crash, and harness_active should return True (skipped peak check)
        self.assertTrue(harness_active(policy))
        self.assertIsNone(policy.timezone)

    def test_eval_mode_off_keeps_explicit_nightly_enabled(self):
        """Legacy fallback: explicit eval.nightly.enabled survives capture off."""
        config = {
            "eval": {
                "enabled": True,
                "capture": {"enabled": False},
                "nightly": {"enabled": True},
            },
        }
        policy = resolve_eval_policy(config)
        self.assertFalse(policy.capture_enabled)
        self.assertTrue(policy.nightly_enabled)

    @patch("eval.policy.datetime")
    def test_peak_hours_wraparound_midnight(self, mock_dt):
        tz = ZoneInfo("America/Halifax")
        config = {
            "eval": {
                "harness": {
                    "enabled": True,
                    "block_peak_hours": True,
                    "peak_start_hour": 22,
                    "peak_end_hour": 7,
                }
            },
            "timezone": "America/Halifax",
        }
        policy = resolve_eval_policy(config)

        mock_dt.now.return_value = datetime(2026, 1, 15, 23, 0, 0, tzinfo=tz)
        self.assertFalse(harness_active(policy))

        mock_dt.now.return_value = datetime(2026, 1, 15, 3, 0, 0, tzinfo=tz)
        self.assertFalse(harness_active(policy))

        mock_dt.now.return_value = datetime(2026, 1, 15, 12, 0, 0, tzinfo=tz)
        self.assertTrue(harness_active(policy))
