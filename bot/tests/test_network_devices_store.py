"""family-bot-dgz: save_network_devices_store writes JSON on the writer role."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import database as db
except ModuleNotFoundError:
    db = None


@unittest.skipUnless(db is not None, "database not available")
class TestSaveNetworkDevicesStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._path = Path(self._tmpdir.name) / "network_devices.json"
        self._old_path = db.NETWORK_DEVICES_PATH
        db.NETWORK_DEVICES_PATH = str(self._path)

    async def asyncTearDown(self):
        db.NETWORK_DEVICES_PATH = self._old_path
        self._tmpdir.cleanup()

    async def test_writes_json_file(self):
        payload = {
            "aa:bb:cc:dd:ee:ff": {"first_seen": "2026-07-08T00:00:00", "vendor": "Test"},
        }
        await db.save_network_devices_store(payload)
        loaded = json.loads(self._path.read_text())
        self.assertEqual(loaded, payload)
