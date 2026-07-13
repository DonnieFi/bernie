import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config_validate import validate_config_core


class TestConfigValidate(unittest.TestCase):
    def _minimal(self) -> dict:
        return {
            "timezone": "America/Halifax",
            "schedule_channel_id": 1,
            "guild_id": 2,
            "poll_interval_minutes": 5,
            "family_members": {
                "Dad": {"canonical_id": "dad", "discord_id": 123},
            },
        }

    def test_valid_config_passes(self):
        validate_config_core(self._minimal())

    def test_missing_timezone_raises(self):
        cfg = self._minimal()
        del cfg["timezone"]
        with self.assertRaises(ValueError) as ctx:
            validate_config_core(cfg)
        self.assertIn("timezone", str(ctx.exception))

    def test_invalid_poll_interval_raises(self):
        cfg = self._minimal()
        cfg["poll_interval_minutes"] = 0
        with self.assertRaises(ValueError):
            validate_config_core(cfg)

    def test_invalid_executor_max_steps_raises(self):
        cfg = self._minimal()
        cfg["executor"] = {"max_steps": 0}
        with self.assertRaises(ValueError):
            validate_config_core(cfg)

    def test_invalid_eval_shadow_daily_cap_raises(self):
        cfg = self._minimal()
        cfg["eval"] = {"shadow_daily_cap": -1}
        with self.assertRaises(ValueError):
            validate_config_core(cfg)

    def test_invalid_eval_capture_defer_s_raises(self):
        cfg = self._minimal()
        cfg["eval"] = {"capture": {"defer_s": -5}}
        with self.assertRaises(ValueError):
            validate_config_core(cfg)

    def test_invalid_eval_harness_peak_hours_raises(self):
        cfg = self._minimal()
        cfg["eval"] = {"harness": {"peak_start_hour": 25}}
        with self.assertRaises(ValueError):
            validate_config_core(cfg)
