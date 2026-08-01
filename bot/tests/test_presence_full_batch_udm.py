"""family-bot-udm: get_full_presence batches last_home_signal; HA locations warm once."""
from __future__ import annotations

import ast
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.utils import _bot


class TestPresenceFullBatchUdmSource(unittest.TestCase):
    def test_get_full_presence_uses_batch_signals(self):
        src = _bot("presence_service.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        # Find get_full_presence body and ensure get_last_home_signals appears,
        # while get_last_home_signal (singular) does not in that method.
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "PresenceService":
                for item in node.body:
                    if isinstance(item, ast.AsyncFunctionDef) and item.name == "get_full_presence":
                        body_src = ast.get_source_segment(src, item) or ""
                        self.assertIn("get_last_home_signals", body_src)
                        # N+1 singular call inside the departing branch must be gone
                        self.assertNotIn(
                            "await get_database().get_last_home_signal(",
                            body_src,
                        )
                        return
        self.fail("PresenceService.get_full_presence not found")

    def test_get_all_person_locations_warms_registry(self):
        src = _bot("ha_service.py").read_text(encoding="utf-8")
        self.assertIn("async def get_all_person_locations", src)
        # Cold registry → refresh_entities once before per-person reads
        start = src.index("async def get_all_person_locations")
        chunk = src[start : start + 800]
        self.assertIn("if not self._states_by_id:", chunk)
        self.assertIn("await self.refresh_entities()", chunk)


class TestPresenceFullBatchUdmBehavior(unittest.IsolatedAsyncioTestCase):
    async def test_departing_uses_batched_signal(self):
        from presence_service import PresenceService

        svc = PresenceService(adapters=[])
        svc._last_states = {
            "alice": {"wifi": False, "gps": False, "essid": None},
        }
        mock_db = MagicMock()
        mock_db.get_presence = AsyncMock(
            return_value={"alice": {"is_home": True, "last_seen": "2026-07-16T12:00:00+00:00"}}
        )
        mock_db.get_last_home_signals = AsyncMock(return_value={"alice": 1e12})  # far future = within grace
        mock_db.get_last_home_signal = AsyncMock(side_effect=AssertionError("N+1 singular call"))

        mock_ha = MagicMock()
        mock_ha.get_all_person_locations = AsyncMock(
            return_value=[{"person_id": "alice", "state": "home", "latitude": None}]
        )

        cfg = {
            "family_members": {
                "Alice": {"canonical_id": "alice", "role": "parent"},
            }
        }
        with patch("presence_service.config", cfg), \
             patch("presence_service.get_database", return_value=mock_db), \
             patch("ha_service.ha_service", mock_ha), \
             patch("constants.registry") as reg:
            reg.resolve = MagicMock(return_value="alice")
            full = await svc.get_full_presence()

        mock_db.get_last_home_signals.assert_awaited_once()
        self.assertTrue(full["alice"]["departing"])
        mock_db.get_last_home_signal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
