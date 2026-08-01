import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config


class TestConfigSync(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.test_dir, "config.json")
        self.initial_data = {
            "timezone": "America/Halifax",
            "guild_id": "12345",
            "schedule_channel_id": "67890",
            "poll_interval_minutes": 10,
            "family_members": {
                "Red": {"web_pin_hash": "abc"}
            },
            "eval": {
                "enabled": True,
                "shadow_model": "haiku",
                "worker_model": "sonnet"
            },
            "frigate": {
                "mode": "on",
                "cameras_enabled": {
                    "cam_1": True
                }
            },
            "executor": {
                "shadow_harness_enabled": False
            }
        }
        with open(self.config_path, "w") as f:
            json.dump(self.initial_data, f, indent=2)

        self.orig_config_path = config._CONFIG_PATH
        config._CONFIG_PATH = self.config_path
        config.config.clear()
        config.config.update(self.initial_data)

    def tearDown(self):
        config._CONFIG_PATH = self.orig_config_path
        shutil.rmtree(self.test_dir)

    def test_deep_merge_correctness(self):
        async def _run():
            await config.update_config({"eval": {"worker_model": "opus"}})
            self.assertEqual(config.config["eval"]["worker_model"], "opus")
            self.assertEqual(config.config["eval"]["shadow_model"], "haiku")
            self.assertTrue(config.config["eval"]["enabled"])
            with open(self.config_path, "r") as f:
                disk_data = json.load(f)
            self.assertEqual(disk_data["eval"]["worker_model"], "opus")
            self.assertEqual(disk_data["eval"]["shadow_model"], "haiku")
            self.assertTrue(disk_data["eval"]["enabled"])
            await config.update_config({"frigate": {"cameras_enabled": {"cam_2": False}}})
            self.assertTrue(config.config["frigate"]["cameras_enabled"]["cam_1"])
            self.assertFalse(config.config["frigate"]["cameras_enabled"]["cam_2"])
            self.assertEqual(config.config["frigate"]["mode"], "on")

        asyncio.run(_run())

    def test_legacy_executor_harness_not_clobbered(self):
        async def _run():
            await config.update_config({"eval": {"harness": {"enabled": True}}})
            self.assertTrue(config.config["eval"]["harness"]["enabled"])
            with open(self.config_path, "r") as f:
                disk_data = json.load(f)
            self.assertTrue(disk_data["eval"]["harness"]["enabled"])
            self.assertEqual(disk_data["executor"]["shadow_harness_enabled"], False)

        asyncio.run(_run())

    def test_empty_updates_guard(self):
        async def _run():
            await config.update_config({})
            self.assertEqual(config.config["eval"]["shadow_model"], "haiku")

        asyncio.run(_run())

    def test_corrupt_file_raises_value_error(self):
        async def _run():
            with open(self.config_path, "w") as f:
                f.write("{invalid_json}")
            with self.assertRaises(ValueError):
                await config.update_config({"eval": {"shadow_model": "gpt-4"}})
            with open(self.config_path, "r") as f:
                content = f.read()
            self.assertEqual(content, "{invalid_json}")

        asyncio.run(_run())

    def test_file_locking_simultaneous_writes(self):
        async def _run():
            tasks = [
                config.update_config({"eval": {"shadow_model": f"model_{i}"}})
                for i in range(5)
            ]
            await asyncio.gather(*tasks)
            with open(self.config_path, "r") as f:
                disk_data = json.load(f)
            self.assertIn("model_", disk_data["eval"]["shadow_model"])
            self.assertEqual(config.config["eval"]["shadow_model"], disk_data["eval"]["shadow_model"])

        asyncio.run(_run())
