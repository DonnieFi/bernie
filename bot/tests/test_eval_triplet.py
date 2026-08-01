import sys, os, asyncio, unittest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from unittest.mock import AsyncMock, MagicMock


class TestFireShadowTriplet(unittest.TestCase):

    def test_fire_shadow_triplet_exists(self):
        from eval_service import fire_shadow_triplet
        self.assertTrue(callable(fire_shadow_triplet))

    def test_fire_shadow_triplet_respects_daily_cap(self):
        from eval_service import fire_shadow_triplet
        db_mock = MagicMock()
        db_mock.get_shadow_call_count_today = AsyncMock(return_value=999)

        async def _run():
            await fire_shadow_triplet(
                user_message="hello",
                system_prompt="system",
                history=[],
                primary_response="primary",
                primary_model="claude-sonnet-4-6",
                shadow_model="claude-haiku-4-5-20251001",
                config={"eval": {"shadow_daily_cap": 20}},
                channel_id="123",
                actor_id="456",
                db_module=db_mock,
                smol_executor=None,
            )
        asyncio.run(_run())
        # Should not store anything when cap is exceeded
        db_mock.store_shadow_triplet.assert_not_called()


if __name__ == "__main__":
    unittest.main()
