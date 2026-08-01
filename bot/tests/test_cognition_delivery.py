"""Gate: cognition/API roles deliver Discord output via cross-container when bot is offline."""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

sys.modules.setdefault("discord", MagicMock())
sys.modules.setdefault("discord.ext", MagicMock())
sys.modules.setdefault("discord.ext.tasks", MagicMock())
sys.modules.setdefault("aiohttp", MagicMock())


class TestPostToAnvil(unittest.IsolatedAsyncioTestCase):
    async def test_uses_cross_container_when_bot_not_ready(self):
        from cross_container import post_to_anvil

        bot = MagicMock()
        bot.is_ready.return_value = False

        with patch("cross_container.discord_client_ready", return_value=False), \
             patch("cross_container.post_to_discord", new_callable=AsyncMock) as mock_post:
            await post_to_anvil("Dead-letter digest body", bot=bot, config={"anvil_channel_id": "999"})

        mock_post.assert_awaited_once_with(999, content="Dead-letter digest body")


class TestHitlCrossContainer(unittest.IsolatedAsyncioTestCase):
    async def test_send_single_hitl_dm_uses_cross_container_when_bot_offline(self):
        from eval.hitl import _send_single_hitl_dm

        row = {
            "id": 42,
            "primary_response": "a",
            "shadow_response": "b",
            "harness_shadow_response": "c",
            "user_message": "hello",
            "prompt_hash": "abc",
        }
        bot = MagicMock()
        bot.is_ready.return_value = False
        db = AsyncMock()
        db.get_tool_calls_for_prompt_hash = AsyncMock(return_value=[])

        from cross_container import PostedMessage

        posted = PostedMessage(12345, 555)
        with patch("cross_container.discord_client_ready", return_value=False), \
             patch("cross_container.post_to_discord", new_callable=AsyncMock, return_value=posted) as mock_post:
            ok = await _send_single_hitl_dm(row, "555", bot, db)

        self.assertTrue(ok)
        mock_post.assert_awaited_once()
        _, kwargs = mock_post.await_args
        self.assertEqual(kwargs["reactions"], ["1️⃣", "2️⃣", "3️⃣", "❌", "⏭️"])
        db.store_shadow_judgment.assert_awaited_once()
        scores = db.store_shadow_judgment.await_args.kwargs["scores"]
        self.assertEqual(scores["dm_message_id"], 12345)


class TestSupervisorCrossContainer(unittest.IsolatedAsyncioTestCase):
    async def test_alert_anvil_falls_back_to_cross_container(self):
        from supervisor import TaskSupervisor

        bot = MagicMock()
        bot.is_ready.return_value = False
        bot.get_channel.return_value = None
        sup = TaskSupervisor(bot)

        with patch("cross_container.post_to_anvil", new_callable=AsyncMock) as mock_post, \
             patch("config.config", {"anvil_channel_id": "888"}):
            await sup._alert_anvil("nightly_eval", "boom", 3)

        mock_post.assert_awaited_once()
        self.assertIn("Supervisor Alert", mock_post.await_args.args[0])


class TestPostedMessage(unittest.IsolatedAsyncioTestCase):
    async def test_add_reaction_calls_internal_react(self):
        from cross_container import PostedMessage

        posted = PostedMessage(99, 123)
        with patch("cross_container.add_message_reaction", new_callable=AsyncMock) as mock_react:
            await posted.add_reaction("✅")
        mock_react.assert_awaited_once_with(123, 99, "✅")


class TestNotificationRouterCrossContainer(unittest.IsolatedAsyncioTestCase):
    async def test_send_discord_uses_cross_container_when_bot_offline(self):
        from cross_container import PostedMessage
        from notification_router import Notification, NotificationRouter

        bot = MagicMock()
        bot.is_ready.return_value = False
        router = NotificationRouter(bot)

        posted = PostedMessage(42, 999)
        with patch("cross_container.post_to_discord", new_callable=AsyncMock, return_value=posted) as mock_post, \
             patch("notification_router.db_writes.routed", new_callable=AsyncMock), \
             patch("notification_router.get_database") as mock_db:
            mock_db.return_value.get_person_pref = AsyncMock(return_value={})
            notif = Notification(recipient_id="999", message="hello", urgency="high")
            result = await router._send_discord(notif)

        mock_post.assert_awaited_once()
        self.assertIs(result, posted)


class TestDiscordReplyDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_discord_reply_uses_delivery_helper(self):
        from cognitive_handlers.handlers.discord_reply import handle_discord_reply

        container = MagicMock()
        task = {
            "id": 1,
            "channel_id": "123",
            "payload": {"topic": "weather", "channel_id": "123", "actor_id": "456"},
        }

        with patch(
            "cognitive_handlers.handlers.discord_reply.call_worker_model",
            new_callable=AsyncMock,
            return_value="sunny",
        ), patch(
            "cognitive_handlers.handlers.discord_reply.deliver_discord_message",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_deliver:
            result = await handle_discord_reply(task, container)

        self.assertEqual(result, {"posted": True, "channel_id": "123"})
        mock_deliver.assert_awaited_once()
        self.assertEqual(mock_deliver.await_args.kwargs.get("mention"), "<@456> ")


if __name__ == "__main__":
    unittest.main()