"""Tests for eval HITL DM sampling visibility and nightly ordering."""
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.modules.setdefault("discord", MagicMock())
sys.modules.setdefault("discord.ext", MagicMock())
sys.modules.setdefault("discord.ext.tasks", MagicMock())

sys.path.append(os.getcwd())
sys.path.append(os.path.join(os.getcwd(), "bot"))

from eval.hitl import handle_hitl_reaction, send_hitl_dms


class TestSendHitlDmsLogging(unittest.IsolatedAsyncioTestCase):
    async def test_skips_when_hitl_disabled(self):
        db = AsyncMock()
        bot = MagicMock()
        config = {
            "admin_discord_id": "123",
            "eval": {"nightly": {"hitl": False}},
        }
        await send_hitl_dms(config, bot, db_module=db)
        db.get_divergent_unsampled_triplets.assert_not_called()
        bot.fetch_user.assert_not_called()

    async def test_logs_when_random_sample_skips_all_candidates(self):
        db = AsyncMock()
        db.get_divergent_unsampled_triplets = AsyncMock(return_value=[
            {"id": 1, "channel_id": "111111111111111111", "primary_response": "a",
             "shadow_response": "b", "harness_shadow_response": "c", "user_message": "hi",
             "prompt_hash": "abc"},
        ])
        bot = MagicMock()
        config = {
            "admin_discord_id": "123",
            "anvil_channel_id": "111111111111111111",
            "eval": {"nightly": {"enabled": True, "hitl": True}},
        }

        with patch("eval.hitl.random.random", return_value=0.99), \
             patch("eval.hitl.log") as mock_log:
            await send_hitl_dms(config, bot, db_module=db)

        info_msgs = [
            (c.args[0] % c.args[1:]) if len(c.args) > 1 else str(c.args[0])
            for c in mock_log.info.call_args_list
        ]
        self.assertTrue(any("sampling 0" in m for m in info_msgs))
        self.assertTrue(any("random sample skipped all" in m for m in info_msgs))
        bot.fetch_user.assert_not_called()

    async def test_logs_sent_count_on_success(self):
        db = AsyncMock()
        db.get_divergent_unsampled_triplets = AsyncMock(return_value=[
            {"id": 2, "channel_id": "999", "primary_response": "a",
             "shadow_response": "b", "harness_shadow_response": "c", "user_message": "hi",
             "prompt_hash": "def", "actor_id": "actor1"},
        ])
        db.get_tool_calls_for_prompt_hash = AsyncMock(return_value=[])
        db.store_shadow_judgment = AsyncMock(return_value=None)

        user = MagicMock()
        dm = MagicMock()
        dm.id = 555
        dm.add_reaction = AsyncMock()
        user.dm_channel = MagicMock()
        user.dm_channel.send = AsyncMock(return_value=dm)
        user.create_dm = AsyncMock(return_value=user.dm_channel)
        bot = MagicMock()
        bot.fetch_user = AsyncMock(return_value=user)
        config = {"admin_discord_id": "123", "anvil_channel_id": "111111111111111111", "eval": {"nightly": {"hitl": True}}}

        with patch("eval.hitl.random.random", return_value=0.0), \
             patch("eval.hitl.log") as mock_log:
            await send_hitl_dms(config, bot, db_module=db)

        info_msgs = [
            (c.args[0] % c.args[1:]) if len(c.args) > 1 else str(c.args[0])
            for c in mock_log.info.call_args_list
        ]
        if mock_log.exception.call_args_list:
            print("MOCK LOG EXCEP:", mock_log.exception.call_args_list[0].kwargs.get("exc_info", True))
        self.assertTrue(any("sent 1/1 HITL survey DM(s)" in m for m in info_msgs))


class TestHitlPendingByMessage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import database as db_mod

        self.db = db_mod
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db_mod.DB_PATH
        db_mod.DB_PATH = os.path.join(self._tmpdir.name, "hitl_lookup.db")
        await db_mod.init_db()

    async def asyncTearDown(self):
        if hasattr(self.db, "close_db"):
            await self.db.close_db()
        self.db.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    async def test_json_extract_avoids_id_prefix_collision(self):
        rid = await self.db.store_shadow_call(
            "shadow", "hash1", "a", "b", "ch", "actor", user_message="hi"
        )
        await self.db.store_shadow_judgment(
            request_id=rid,
            judge_kind="hitl_pending",
            winner=None,
            scores={"dm_message_id": 12345, "shuffle_order": [0, 1, 2]},
            actor_id="admin",
        )
        self.assertIsNone(await self.db.get_hitl_pending_by_message(12))
        row = await self.db.get_hitl_pending_by_message(12345)
        self.assertIsNotNone(row)
        self.assertEqual(row["judge_kind"], "hitl_pending")


class TestHandleHitlReaction(unittest.IsolatedAsyncioTestCase):
    async def test_skips_when_hitl_disabled(self):
        db = AsyncMock()
        await handle_hitl_reaction(
            message_id=999,
            emoji="1️⃣",
            actor_id="admin",
            db_module=db,
            config={"eval": {"nightly": {"hitl": False}}},
        )
        db.get_hitl_pending_by_message.assert_not_called()
        db.store_shadow_judgment.assert_not_called()


class TestNightlyEvalHitlOrdering(unittest.IsolatedAsyncioTestCase):
    async def test_hitl_runs_before_anvil_digest(self):
        import eval.nightly as nightly_mod

        db = AsyncMock()
        db.get_unscored_shadow_calls = AsyncMock(return_value=[])
        db.get_unscored_triplets = AsyncMock(return_value=[])
        call_order: list[str] = []

        async def _hitl(*_a, **_kw):
            call_order.append("hitl")

        mock_router = AsyncMock()
        mock_router.notification = lambda **kw: MagicMock(**kw)

        async def _notify(*_a, **_kw):
            call_order.append("anvil")
            return {"discord": True}

        mock_router.notify = _notify

        svc = MagicMock()
        svc.send_hitl_dms = AsyncMock(side_effect=_hitl)
        svc.audit_ungrounded_live_data = AsyncMock(return_value=[])
        svc.format_ungrounded_audit_section = MagicMock(return_value="")

        config = {"eval": {"enabled": True, "shadow_model": "or-test"}, "anvil_channel_id": "1"}
        bot = MagicMock()

        with patch.object(nightly_mod, "get_database", return_value=db), \
             patch.object(nightly_mod, "_eval_service", return_value=svc), \
             patch.object(nightly_mod, "build_nightly_summary", return_value="summary"):
            await nightly_mod.nightly_eval_worker(
                config, orchestrator=mock_router, bot_instance=bot,
            )

        self.assertEqual(call_order, ["hitl", "anvil"])


if __name__ == "__main__":
    unittest.main()