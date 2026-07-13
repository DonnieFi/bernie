import unittest
import asyncio
import json
import sys
import os
import struct
import tempfile
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone, timedelta

# Mock missing modules before they are imported by Bernie modules
sys.modules["discord"] = MagicMock()
sys.modules["discord.ext"] = MagicMock()
sys.modules["discord.ext.tasks"] = MagicMock()
sys.modules["anthropic"] = MagicMock()
sys.modules["websockets"] = MagicMock()
sys.modules["google.oauth2.credentials"] = MagicMock()
sys.modules["google.auth.transport.requests"] = MagicMock()
sys.modules["googleapiclient.discovery"] = MagicMock()

import sys
sys.path.append(os.getcwd())
sys.path.append(os.path.join(os.getcwd(), "bot"))

# Now we can import
import database
from supervisor import TaskSupervisor
from watchman import Watchman
from notification_router import NotificationRouter, Notification

class TestPhase24Infrastructure(unittest.IsolatedAsyncioTestCase):
    
    async def asyncSetUp(self):
        self.bot = MagicMock()
        self.bot.user = MagicMock()
        self.bot.user.name = "Bernie"
        # Isolate from the live prod DB. Without this the suite wrote to
        # /data/family_bot.db (lock contention with the running containers) and
        # left aiosqlite's non-daemon worker thread alive — hanging interpreter
        # exit (RC=124) with "Event loop is closed" tracebacks. Mirrors the
        # temp-DB + close_db() pattern in test_phase13_db / test_identity.
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self._tmpdir.name, "phase24_test.db")
        await database.init_db()

    async def asyncTearDown(self):
        if hasattr(database, "close_db"):
            await database.close_db()
        database.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    def test_supervisor_registration(self):
        """Verify TaskSupervisor correctly registers and tracks tasks."""
        sv = TaskSupervisor(self.bot)
        mock_loop = MagicMock()
        mock_loop.is_running.return_value = False
        
        sv.register("test_task", mock_loop)
        status = sv.get_status()
        
        self.assertIn("test_task", status["tasks"])
        self.assertEqual(status["tasks"]["test_task"]["status"], "registered")

    async def test_supervisor_health_updates(self):
        """Verify TaskSupervisor updates health metrics correctly."""
        sv = TaskSupervisor(self.bot)
        sv.register("test_task", MagicMock())

        # Success update
        sv.update_health("test_task")
        status = sv.get_status()["tasks"]["test_task"]
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["run_count"], 1)

        # Error update (triggers auto-restart)
        sv.update_health("test_task", error=ValueError("Boom"))
        status = sv.get_status()["tasks"]["test_task"]
        self.assertEqual(status["status"], "restarting") # Updated from 'degraded'
        self.assertEqual(status["errors"], 1)
        self.assertEqual(status["last_error"], "Boom")

    async def test_supervisor_failed_field_falls_back_to_status(self):
        """BTS wraps loops so they swallow exceptions; loop.failed() is
        therefore always False post-migration. get_status() must still
        report failed=True when the task has been degraded out of the
        auto-restart budget. Async because update_health spawns an
        #anvil alert task once the restart budget is exhausted."""
        sv = TaskSupervisor(self.bot)
        mock_loop = MagicMock()
        mock_loop.is_running.return_value = True
        mock_loop.failed.return_value = False   # what BTS-wrapped loops always return
        mock_loop.current_loop = 1
        sv.register("test_task", mock_loop)
        # Burn through restart budget so status flips to "degraded".
        for _ in range(5):
            sv.update_health("test_task", error=ValueError("boom"))
        status = sv.get_status()["tasks"]["test_task"]
        self.assertEqual(status["status"], "degraded")
        self.assertTrue(status["failed"], "failed must reflect degraded state, not loop.failed()")

    @patch("watchman.Watchman._docker_request", new_callable=AsyncMock)
    async def test_watchman_log_collection(self, mock_request):
        """Verify Watchman correctly filters logs from Docker API (using multiplexed stream headers)."""
        
        # Helper to create a Docker Stream frame
        def make_frame(stream_type, text):
            payload = text.encode("utf-8")
            header = struct.pack(">BxxxL", stream_type, len(payload))
            return header + payload

        log_binary = (
            make_frame(1, "INFO: fine\n") + 
            make_frame(2, "ERROR: bad thing\n") + 
            make_frame(2, "Exception: boom\n")
        )

        # 1. Mock container list
        mock_request.side_effect = [
            # List containers
            [{"Names": ["/test-service"], "Id": "123", "Status": "running", "State": "running", "Image": "test:latest"}],
            # Fetch logs (raw binary)
            log_binary
        ]
        
        # Mock config to include test-service in monitored_containers
        with patch("watchman.config", {"watchman": {"monitored_containers": ["test-service"]}}):
            wm = Watchman()
            # Force docker available for test
            wm._docker_available = True
            errors = await wm.get_recent_errors(hours=24)
        
        self.assertIn("test-service", errors)
        self.assertEqual(len(errors["test-service"]), 2)
        self.assertIn("ERROR: bad thing", errors["test-service"])
        self.assertIn("Exception: boom", errors["test-service"])

    @patch("database.log_notification", new_callable=AsyncMock)
    @patch("presence_service.presence_service.get_full_presence", new_callable=AsyncMock)
    @patch("notification_router._is_quiet_hours", return_value=False)
    async def test_normal_delivers_immediately_regardless_of_presence(self, mock_quiet, mock_presence, mock_log_notif):
        """Being away must NOT queue. Normal notifications deliver immediately and
        presence is never consulted on the delivery path (the old behavior caused a
        spam burst on every return home)."""
        with patch("constants.registry.resolve", return_value="dad"):
            router = NotificationRouter(self.bot)
            router._send_discord = AsyncMock(return_value=True)
            notif = Notification(recipient_id="123", message="Hello", urgency="normal")

            res = await router.notify(notif)

            router._send_discord.assert_awaited_once()
            self.assertNotIn(res.get("status"), ("queued", "queued_quiet_hours"))
            mock_presence.assert_not_called()  # presence fully decoupled from delivery

    @patch("notification_router.db_writes.routed", new_callable=AsyncMock)
    @patch("notification_router._has_explicit_prefs_row", new_callable=AsyncMock, return_value=False)
    @patch("database.get_person_pref", new_callable=AsyncMock, return_value={})
    @patch("notification_router._is_quiet_hours", return_value=True)
    async def test_normal_queued_during_quiet_hours(self, mock_quiet, mock_pref, mock_row, mock_routed):
        """Quiet hours still queue normal notifications — the only suppression path."""
        with patch("constants.registry.resolve", return_value="dad"):
            router = NotificationRouter(self.bot)
            router._send_discord = AsyncMock(return_value=True)
            notif = Notification(recipient_id="123", message="Hello", urgency="normal")

            res = await router.notify(notif)

            self.assertEqual(res["status"], "queued_quiet_hours")
            mock_routed.assert_awaited_once()
            self.assertEqual(mock_routed.await_args.args[0], "add_pending_notification")
            router._send_discord.assert_not_called()

    @patch("notification_router.db_writes.routed", new_callable=AsyncMock)
    @patch("notification_router._is_quiet_hours", return_value=True)
    async def test_high_urgency_bypasses_quiet_hours(self, mock_quiet, mock_routed):
        """High urgency delivers even during quiet hours (security/urgent path)."""
        router = NotificationRouter(self.bot)
        router._send_discord = AsyncMock(return_value=True)
        notif = Notification(recipient_id="123", message="Alarm", urgency="high")

        await router.notify(notif)

        router._send_discord.assert_awaited_once()
        queued = [
            c for c in mock_routed.await_args_list
            if c.args and c.args[0] == "add_pending_notification"
        ]
        self.assertEqual(queued, [])

    @patch("notification_router.db_writes.routed", new_callable=AsyncMock)
    @patch("database.list_pending_notifications", new_callable=AsyncMock)
    async def test_notification_router_flushing(self, mock_db_list, mock_routed):
        """Verify NotificationRouter flushes queued messages via routed write."""
        mock_db_list.return_value = [
            {"id": 1, "message": "Msg 1", "title": "T1", "embed_json": None},
            {"id": 2, "message": "Msg 2", "title": "T2", "embed_json": None}
        ]
        
        router = NotificationRouter(self.bot)
        router._send_discord = AsyncMock(return_value=True)
        
        await router.flush_pending("123")
        
        self.assertEqual(router._send_discord.call_count, 2)
        mock_routed.assert_called_once_with(
            "clear_pending_notifications_by_ids", ids=[1, 2]
        )

    @patch("llm.ollama.call_ollama", new_callable=AsyncMock)
    async def test_ask_ollama_tool(self, mock_ollama):
        """Verify the ask_ollama tool logic in claude_service."""
        from llm.compat import execute_tool as _execute_tool
        
        mock_ollama.return_value = "Ollama response"
        tool_input = {"query": "What time is it?", "system_prompt": "Be precise"}
        
        # async def _execute_tool(tool_name, tool_input, config, cal_service, db_module, tz, session, notification_router=None, group=None)
        res = await _execute_tool(
            "ask_ollama", tool_input, 
            config={}, cal_service=None, db_module=MagicMock(), 
            tz=MagicMock(), session=MagicMock(),
            group="admin"
        )
        
        self.assertEqual(res, "Ollama response")
        mock_ollama.assert_called_once()
        args, kwargs = mock_ollama.call_args
        self.assertEqual(args[0], "Be precise") # system prompt

    @patch("ha_service.ha_service.get_state", new_callable=AsyncMock)
    async def test_watchman_remote_health(self, mock_get_state):
        """Verify Watchman correctly polls and labels HA Pi-hole status."""
        def side_effect(eid):
            if eid == "binary_sensor.pihole_aka_status":
                return {"state": "on"}
            if eid == "binary_sensor.pihole_suji_status":
                return {"state": "off"}
            return None
        
        mock_get_state.side_effect = side_effect
        
        # Inject entity IDs via config (production path)
        entities = ["binary_sensor.pihole_aka_status", "binary_sensor.pihole_suji_status"]
        with patch("watchman.config", {"watchman": {"health_entities": entities}}):
            wm = Watchman()
            results = await wm.get_remote_health()
        
        self.assertEqual(results["binary_sensor.pihole_aka_status"], "Healthy")
        self.assertEqual(results["binary_sensor.pihole_suji_status"], "Offline")
        self.assertEqual(mock_get_state.call_count, 2)

    @patch("ha_service.ha_service.get_state", new_callable=AsyncMock)
    async def test_watchman_remote_health_no_entities(self, mock_get_state):
        """When no health_entities are configured, get_remote_health returns {} without calling HA."""
        with patch("watchman.config", {"watchman": {"health_entities": []}}):
            wm = Watchman()
            results = await wm.get_remote_health()

        self.assertEqual(results, {})
        mock_get_state.assert_not_called()

    @patch("ha_service.ha_service.get_state", new_callable=AsyncMock)
    async def test_icloud3_sensor_polling(self, mock_get_state):
        """Verify that '3' series (iCloud3) sensors are polled and correctly labeled."""
        from ha_service import ha_service

        def side_effect(eid):
            if "device_tracker.calla3" in eid:
                return {"state": "SacredHeart", "attributes": {"friendly_name": "Child1 iCloud3", "latitude": 44.641, "longitude": -63.582}}
            if "sensor.child1_cloud3_battery" in eid:
                return {"state": "95"}
            return {"state": "unknown", "attributes": {}}

        mock_get_state.side_effect = side_effect

        # Poll Child1's location
        with patch("ha_service.config", {
            "presence": {
                "device_trackers": {
                    "child1": {
                        "device_tracker": "device_tracker.calla3_child1_cloud3_calla3",
                        "battery_sensor": "sensor.child1_cloud3_battery"
                    }
                }
            }
        }):
            res = await ha_service.get_person_location("child1")

        self.assertEqual(res["state"], "SacredHeart")
        self.assertEqual(res["battery"], "95")
        self.assertEqual(res["latitude"], 44.641)

    @patch("database.get_task", new_callable=AsyncMock)
    @patch("database.list_all_tasks", new_callable=AsyncMock)
    async def test_fuzzy_task_resolution(self, mock_list, mock_get):
        """Verify _find_task correctly resolves by ID or title."""
        from llm.compat import find_task as _find_task
        
        # 1. Resolve by ID
        mock_get.return_value = {"id": 10, "title": "Buy Milk"}
        res = await _find_task(database, task_id=10, title_search=None, actor_id="dad")
        self.assertEqual(res["id"], 10)
        
        # 2. Resolve by Title (Unique)
        mock_list.return_value = [
            {"id": 10, "title": "Buy Milk", "assigned_to": "dad"},
            {"id": 11, "title": "Mow Lawn", "assigned_to": "dad"}
        ]
        res = await _find_task(database, task_id=None, title_search="milk", actor_id="dad")
        self.assertEqual(res["id"], 10)
        
        # 3. Resolve by Title (Multiple - returns list/message)
        mock_list.return_value = [
            {"id": 10, "title": "Buy Milk", "assigned_to": "dad"},
            {"id": 12, "title": "Buy Eggs", "assigned_to": "dad"}
        ]
        res = await _find_task(database, task_id=None, title_search="buy", actor_id="dad")
        self.assertIn("Found 2 matches", res)

    async def test_rbac_enforcement_in_tool_loop(self):
        """Verify that kids cannot call admin/parent restricted tools."""
        from llm.compat import execute_tool as _execute_tool
        
        # 1. Admin tool called by kid
        res = await _execute_tool(
            "get_system_health", {}, 
            config={}, cal_service=None, db_module=database, 
            tz=MagicMock(), session=MagicMock(),
            group="kids"
        )
        self.assertIn("restricted to parents or admins", res)
        
        # 2. Parent tool called by kid
        res = await _execute_tool(
            "create_task", {"title": "X", "assigned_to": "dad"}, 
            config={}, cal_service=None, db_module=database, 
            tz=MagicMock(), session=MagicMock(),
            group="kids"
        )
        self.assertIn("restricted to parents or admins", res)

        # 3. Admin tool called by parent
        res = await _execute_tool(
            "reload_config", {}, 
            config={}, cal_service=None, db_module=database, 
            tz=MagicMock(), session=MagicMock(),
            group="parents"
        )
        self.assertIn("Access Denied", res)
        self.assertIn("requires the 'admin' role", res)

    @patch("database.delete_automation", new_callable=AsyncMock)
    async def test_delete_automation_tool(self, mock_delete):
        """Verify delete_automation tool logic."""
        from llm.compat import execute_tool as _execute_tool
        
        res = await _execute_tool(
            "delete_automation", {"id": 123}, 
            config={}, cal_service=None, db_module=database, 
            tz=MagicMock(), session=MagicMock(),
            group="admin",
            hitl_approved=True,
        )
        
        self.assertIn("deleted permanently", res)
        mock_delete.assert_called_once_with(123)

    @patch("identity_service.identity_service.get_identity_info", new_callable=AsyncMock)
    async def test_get_identity_info_tool(self, mock_info):
        """get_identity_info tool returns formatted evidence chain; falls back to PersonRegistry on miss."""
        from llm.compat import execute_tool as _execute_tool

        # Hit: identity graph returns a result
        mock_info.return_value = {
            "canonical_id": "dad",
            "confidence": 0.95,
            "evidence": [
                {"alias": "dad", "source": "config", "verified": True, "added_at": "2026-01-01"},
                {"alias": "dad", "source": "config", "verified": True, "added_at": "2026-01-01"},
            ],
            "error": None,
        }
        res = await _execute_tool(
            "get_identity_info", {"query": "Dad"},
            config={}, cal_service=None, db_module=MagicMock(),
            tz=MagicMock(), session=MagicMock(), group="all"
        )
        self.assertIn("dad", res)
        self.assertIn("0.95", res)
        self.assertIn("dad", res)

        # Miss: graph returns error, PersonRegistry fallback fires
        mock_info.return_value = {"canonical_id": None, "confidence": 0.0, "evidence": [], "error": "not found"}
        res_miss = await _execute_tool(
            "get_identity_info", {"query": "nobody"},
            config={}, cal_service=None, db_module=MagicMock(),
            tz=MagicMock(), session=MagicMock(), group="all"
        )
        self.assertIn("nobody", res_miss)

    @patch("identity_service.identity_service.resolve_entity", new_callable=AsyncMock)
    async def test_resolve_entity_tool(self, mock_resolve):
        """resolve_entity tool returns canonical_id + confidence; falls back on miss."""
        from llm.compat import execute_tool as _execute_tool

        mock_resolve.return_value = {
            "canonical_id": "mom", "confidence": 0.95,
            "source": "config", "verified": True,
        }
        res = await _execute_tool(
            "resolve_entity", {"key": "Mom"},
            config={}, cal_service=None, db_module=MagicMock(),
            tz=MagicMock(), session=MagicMock(), group="all"
        )
        self.assertIn("mom", res)
        self.assertIn("verified", res)

        # Miss
        mock_resolve.return_value = None
        res_miss = await _execute_tool(
            "resolve_entity", {"key": "stranger"},
            config={}, cal_service=None, db_module=MagicMock(),
            tz=MagicMock(), session=MagicMock(), group="all"
        )
        self.assertIn("stranger", res_miss)

if __name__ == "__main__":
    unittest.main()
