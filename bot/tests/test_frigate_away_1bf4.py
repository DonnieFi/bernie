"""family-bot-1bf.4: Frigate away-gate uses lightweight presence."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch


class TestFrigateAwayGate(unittest.IsolatedAsyncioTestCase):
    async def test_is_away_false_when_parent_home(self):
        from frigate_listener import _is_away

        with (
            patch(
                "frigate_listener.config",
                {
                    "family_members": {
                        "Dad": {"canonical_id": "dad", "role": "admin"},
                    }
                },
            ),
            patch("presence_service.presence_service.is_any_home", new_callable=AsyncMock) as m,
        ):
            m.return_value = True
            self.assertFalse(await _is_away())
            m.assert_awaited_once()
            self.assertEqual(m.await_args.args[0], ["dad"])

    async def test_is_away_true_when_no_parent_home(self):
        from frigate_listener import _is_away

        with (
            patch(
                "frigate_listener.config",
                {
                    "family_members": {
                        "Dad": {"canonical_id": "dad", "role": "parents"},
                    }
                },
            ),
            patch("presence_service.presence_service.is_any_home", new_callable=AsyncMock, return_value=False),
        ):
            self.assertTrue(await _is_away())

    async def test_is_away_true_on_error(self):
        from frigate_listener import _is_away

        with (
            patch(
                "frigate_listener.config",
                {"family_members": {"Dad": {"canonical_id": "dad", "role": "admin"}}},
            ),
            patch(
                "presence_service.presence_service.is_any_home",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db down"),
            ),
        ):
            self.assertTrue(await _is_away())

    async def test_is_any_home_reads_db_only(self):
        from presence_service import PresenceService

        svc = PresenceService(adapters=[])
        with patch("presence_service.get_database") as gdb:
            gdb.return_value.get_presence = AsyncMock(
                return_value={"dad": {"is_home": True}, "mom": {"is_home": False}}
            )
            self.assertTrue(await svc.is_any_home(["dad", "mom"]))
            self.assertFalse(await svc.is_any_home(["mom"]))
            self.assertFalse(await svc.is_any_home([]))


if __name__ == "__main__":
    unittest.main()
