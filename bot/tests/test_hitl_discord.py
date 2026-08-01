import sys
import types
import importlib
from unittest.mock import MagicMock, AsyncMock, patch
sys.modules['audioop'] = MagicMock()

import os
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _ensure_real_discord() -> None:
    """Prior staged modules (e.g. test_frigate_hours) stub sys.modules['discord'] at import."""
    if isinstance(sys.modules.get("discord"), MagicMock):
        for key in list(sys.modules):
            if key == "discord" or key.startswith("discord."):
                del sys.modules[key]
        import discord
        import discord.ext
        import discord.ext.tasks

        sys.modules["discord"] = discord
        sys.modules["discord.ui"] = discord.ui
        sys.modules["discord.ext"] = discord.ext
        sys.modules["discord.ext.tasks"] = discord.ext.tasks


_ensure_real_discord()

import hitl.hitl_discord as _hitl_discord_mod
importlib.reload(_hitl_discord_mod)

import database as test_db
from db_binding import bind_database
from executor import ToolContext, ServiceRefs
from hitl.hitl_discord import (
    HitlApprovalView,
    build_hitl_embed,
    resolve_admin_discord_ids,
    register_pending_hitl_views,
    send_hitl_approval_dms,
    sync_sibling_dm_cards,
    init_production_refs,
    _truncate_audit_text,
)


class TestHitlDiscord(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_role = os.environ.get("ROLE")
        os.environ["ROLE"] = "monolith"
        # Temp DB setup
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._old_db_path = test_db.DB_PATH
        test_db.DB_PATH = self._tmp.name
        bind_database(test_db)
        await test_db.init_db()

        import llm.runtime as runtime
        self._prev_runtime_container = runtime._container
        runtime._container = None

        self.services = ServiceRefs(db=test_db)
        self.gateway = AsyncMock()

        # hitl_discord caches gateway/services after first init_production_refs()
        import hitl.hitl_discord as hd
        hd._production_gateway = None
        hd._production_services = None

        from hitl.hitl_discord import get_inline_notifier, set_inline_notifier

        self._old_inline_notifier = get_inline_notifier()
        set_inline_notifier(AsyncMock())

    async def asyncTearDown(self):
        from hitl.hitl_discord import set_inline_notifier

        set_inline_notifier(self._old_inline_notifier)
        if self._old_role is None:
            os.environ.pop("ROLE", None)
        else:
            os.environ["ROLE"] = self._old_role
        import llm.runtime as runtime
        runtime._container = self._prev_runtime_container
        test_db.DB_PATH = self._old_db_path
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def _mock_interaction(self, is_admin=True, user_name="dad", user_id=123):
        interaction = MagicMock()
        interaction.user = MagicMock()
        interaction.user.name = user_name
        interaction.user.id = user_id
        interaction.user.display_name = user_name.capitalize()
        interaction.client = None
        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()
        interaction.message.embeds = []
        
        return interaction

    async def test_admin_allowed_non_admin_rejected(self):
        # Create a pending row
        pending_id = await test_db.create_pending_hitl(
            tool_name="test_tool",
            args_json="{}",
            ctx_json="{}",
            expires_at="2036-06-06T12:00:00Z",
            requested_at="2026-06-06T11:55:00Z",
        )

        view = HitlApprovalView(pending_id=pending_id, gateway=self.gateway, services=self.services)

        # 1. Non-admin user click
        non_admin_interaction = self._mock_interaction(is_admin=False, user_name="child2", user_id=456)
        with patch("hitl.hitl_discord.resolve_admin_discord_ids", return_value=[123]):
            await view.approve_callback(non_admin_interaction)

        # Check warning was sent
        non_admin_interaction.response.send_message.assert_called_once()
        self.assertIn("You must be an admin", non_admin_interaction.response.send_message.call_args[0][0])

        # 2. Admin user click
        admin_interaction = self._mock_interaction(is_admin=True, user_name="dad", user_id=123)
        with patch("hitl.hitl_discord.resolve_admin_discord_ids", return_value=[123]), \
             patch("hitl.hitl_discord.resume_pending", return_value="write_ok") as mock_resume:
            await view.approve_callback(admin_interaction)

        # Check it deferred and resumed
        admin_interaction.response.defer.assert_called_once()
        mock_resume.assert_called_once()

    async def test_approve_calls_resume_pending(self):
        pending_id = await test_db.create_pending_hitl(
            tool_name="test_tool",
            args_json="{}",
            ctx_json="{}",
            expires_at="2036-06-06T12:00:00Z",
            requested_at="2026-06-06T11:55:00Z",
        )

        view = HitlApprovalView(pending_id=pending_id, gateway=self.gateway, services=self.services)
        interaction = self._mock_interaction(is_admin=True)

        with patch("hitl.hitl_discord.resolve_admin_discord_ids", return_value=[123]), \
             patch("hitl.hitl_discord.resume_pending", return_value="success_result") as mock_resume, \
             patch("constants.registry.resolve", return_value="dad"):
            await view.approve_callback(interaction)

        mock_resume.assert_called_once_with(
            pending_id, self.gateway, services=self.services, decided_by="dad"
        )
        interaction.followup.send.assert_called_with("Request approved. Result:\nsuccess_result", ephemeral=True)

    async def test_deny_does_not_dispatch(self):
        pending_id = await test_db.create_pending_hitl(
            tool_name="test_tool",
            args_json="{}",
            ctx_json="{}",
            expires_at="2036-06-06T12:00:00Z",
            requested_at="2026-06-06T11:55:00Z",
        )

        view = HitlApprovalView(pending_id=pending_id, gateway=self.gateway, services=self.services)
        interaction = self._mock_interaction(is_admin=True)

        with patch("hitl.hitl_discord.resolve_admin_discord_ids", return_value=[123]), \
             patch("constants.registry.resolve", return_value="dad"):
            await view.deny_callback(interaction)

        # Verify DB status is denied
        row = await test_db.get_pending_hitl(pending_id)
        self.assertEqual(row["status"], "denied")
        self.assertEqual(row["decided_by"], "dad")

        # Verify no tool execution was dispatched
        self.gateway.execute.assert_not_called()
        interaction.followup.send.assert_called_with("Request denied.", ephemeral=True)

    async def test_check_admin_fail_closed_without_bot_import(self):
        pending_id = await test_db.create_pending_hitl(
            tool_name="test_tool",
            args_json="{}",
            ctx_json="{}",
            expires_at="2036-06-06T12:00:00Z",
            requested_at="2026-06-06T11:55:00Z",
        )
        view = HitlApprovalView(pending_id=pending_id, gateway=self.gateway, services=self.services)
        outsider = self._mock_interaction(is_admin=False, user_id=99999)
        admin = self._mock_interaction(is_admin=True, user_id=111)

        empty_bot = types.ModuleType("bot")
        with patch.dict(sys.modules, {"bot": empty_bot}), patch(
            "config.load_config",
            return_value={"admin_discord_ids": [111]},
        ):
            await view.approve_callback(outsider)
            outsider.response.send_message.assert_called_once()
            self.assertIn("You must be an admin", outsider.response.send_message.call_args[0][0])

            with patch("hitl.hitl_discord.resume_pending", return_value="ok"):
                await view.approve_callback(admin)
            admin.response.defer.assert_called_once()

    async def test_resolve_admin_discord_ids(self):
        config = {
            "family_members": {
                "Dad": {"role": "admin", "discord_id": 111},
                "Mom": {"role": "admin", "discord_id": "222"},
                "Child2": {"role": "child", "discord_id": 333},
            }
        }
        admins = resolve_admin_discord_ids(config)
        self.assertEqual(sorted(admins), [111, 222])

        # Test override
        config_override = {
            "admin_discord_ids": [999, "888"],
            "family_members": {
                "Dad": {"role": "admin", "discord_id": 111},
            }
        }
        admins_override = resolve_admin_discord_ids(config_override)
        self.assertEqual(sorted(admins_override), [888, 999])

        # Legacy singular fallback when no admin role in family_members
        legacy_config = {"admin_discord_id": 123456789012345678, "family_members": {}}
        self.assertEqual(resolve_admin_discord_ids(legacy_config), [123456789012345678])

    async def test_on_ready_reregistration(self):
        # Insert two pending requests
        await test_db.create_pending_hitl("tool_a", "{}", "{}", expires_at="2036-06-06T12:00:00Z", requested_at="2026-06-06T11:55:00Z")
        await test_db.create_pending_hitl("tool_b", "{}", "{}", expires_at="2036-06-06T12:00:00Z", requested_at="2026-06-06T11:55:00Z")
        # Insert one resolved request (should be ignored)
        resolved_id = await test_db.create_pending_hitl("tool_c", "{}", "{}", expires_at="2036-06-06T12:00:00Z", requested_at="2026-06-06T11:55:00Z")
        await test_db.resolve_pending_hitl(resolved_id, "approved", "admin")

        bot = MagicMock()
        bot.add_view = MagicMock()
        bot._container = None

        with patch("hitl.hitl_discord.init_production_refs"), \
             patch("hitl.hitl_discord._get_production_gateway", return_value=self.gateway), \
             patch("hitl.hitl_discord._get_production_services", return_value=self.services):
            await register_pending_hitl_views(bot)

        # add_view should be called exactly twice (for the two pending requests)
        self.assertEqual(bot.add_view.call_count, 2)
        added_views = [call[0][0] for call in bot.add_view.call_args_list]
        self.assertEqual(added_views[0].pending_id, 1)
        self.assertEqual(added_views[1].pending_id, 2)

    def test_truncate_audit_text(self):
        short = _truncate_audit_text("hello")
        self.assertEqual(short, "hello")
        long_text = "x" * 2000
        truncated = _truncate_audit_text(long_text)
        self.assertLessEqual(len(truncated), 1900)
        self.assertIn("[...truncated]", truncated)

    async def test_send_hitl_approval_dms_stores_admin_map(self):
        pending_id = await test_db.create_pending_hitl(
            "test_tool", "{}", '{"person_id": "dad"}',
            expires_at="2036-06-06T12:00:00Z", requested_at="2026-06-06T11:55:00Z",
        )

        mock_msg = MagicMock()
        mock_msg.id = 5555
        mock_channel = AsyncMock()
        mock_channel.send = AsyncMock(return_value=mock_msg)
        mock_user = MagicMock()
        mock_user.dm_channel = mock_channel
        mock_user.create_dm = AsyncMock(return_value=mock_channel)

        bot = MagicMock()
        bot.get_user = MagicMock(return_value=mock_user)
        bot.fetch_user = AsyncMock(return_value=mock_user)

        with patch.dict(os.environ, {"BERNIE_DISABLE_HITL_DM": "0"}, clear=False), \
             patch("hitl.hitl_discord.resolve_admin_discord_ids", return_value=[101]), \
             patch("config.load_config", return_value={}), \
             patch("hitl.hitl_discord._get_production_gateway", return_value=self.gateway), \
             patch("hitl.hitl_discord._get_production_services", return_value=self.services):
            sent = await send_hitl_approval_dms(pending_id, bot)

        self.assertEqual(sent, [(101, 5555)])
        row = await test_db.get_pending_hitl(pending_id)
        mapping = test_db.parse_pending_hitl_notify_map(row["notify_message_ids"])
        self.assertEqual(mapping, {101: 5555})

    async def test_orphan_notify_on_register(self):
        pending_id = await test_db.create_pending_hitl(
            "tool_a", "{}", "{}", expires_at="2036-06-06T12:00:00Z", requested_at="2026-06-06T11:55:00Z",
        )

        bot = MagicMock()
        bot.add_view = MagicMock()

        with patch("hitl.hitl_discord.init_production_refs"), \
             patch("hitl.hitl_discord._get_production_gateway", return_value=self.gateway), \
             patch("hitl.hitl_discord._get_production_services", return_value=self.services), \
             patch("hitl.hitl_discord.send_hitl_approval_dms", new_callable=AsyncMock) as mock_dm:
            await register_pending_hitl_views(bot)

        mock_dm.assert_awaited_once_with(pending_id, bot)

    async def test_sync_sibling_dm_cards(self):
        pending_id = await test_db.create_pending_hitl(
            "tool", "{}", "{}", expires_at="2036-06-06T12:00:00Z", requested_at="2026-06-06T11:55:00Z",
        )
        await test_db.set_pending_hitl_notify_message_ids(pending_id, [(101, 9001), (202, 9002)])

        embed = MagicMock()
        embed.set_footer = MagicMock()
        msg = MagicMock()
        msg.id = 9001
        msg.embeds = [embed]
        msg.edit = AsyncMock()

        dm = MagicMock()
        dm.fetch_message = AsyncMock(return_value=msg)
        user = MagicMock()
        user.dm_channel = dm
        user.create_dm = AsyncMock(return_value=dm)

        bot = MagicMock()
        bot.get_user = MagicMock(return_value=user)
        bot.fetch_user = AsyncMock(return_value=user)

        with patch("hitl.hitl_discord._get_production_gateway", return_value=self.gateway), \
             patch("hitl.hitl_discord._get_production_services", return_value=self.services):
            await sync_sibling_dm_cards(
                bot, pending_id, suffix="Denied by dad", exclude_message_id=9002,
            )

        dm.fetch_message.assert_awaited_once_with(9001)
        msg.edit.assert_awaited_once()
