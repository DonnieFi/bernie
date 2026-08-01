import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from store.task_store import InMemoryTaskStore
from services.unified_task_service import UnifiedTaskService, TaskValidationError
from service_container import ServiceContainer
from executor import ServiceRefs

_TASK_TYPES = {
    "research": ["agent:bernie"],
    "bernie": ["agent:bernie"],
    "code": ["agent:nanobot"],
    "chore": ["person:*"],
}


class TestUnifiedTaskService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import database as _db_mod
        from db_binding import bind_database
        bind_database(_db_mod)

        self.store = InMemoryTaskStore()
        self.enqueue_patcher = patch.object(
            UnifiedTaskService, "enqueue_research_run", new_callable=AsyncMock,
        )
        self.mock_enqueue = self.enqueue_patcher.start()

        # Mock person registry with standard aliases/persons
        self.mock_registry = MagicMock()
        self.mock_registry.resolve.side_effect = lambda name: name.lower() if name else None

        self.config = {"task_types": _TASK_TYPES}
        self.service = UnifiedTaskService(
            task_store=self.store,
            person_registry=self.mock_registry,
            config=self.config
        )

    async def asyncTearDown(self):
        self.enqueue_patcher.stop()

    async def test_invalid_assignee_rejected(self):
        with self.assertRaises(TaskValidationError) as ctx:
            await self.service.create_agent_task(
                task_type="research",
                title="Search dentists",
                assigned_to="person:child2",  # Not in allowed list for research
                assigned_by="agent:bernie"
            )
        self.assertIn("not permitted", str(ctx.exception).lower())

    async def test_empty_title_rejected(self):
        with self.assertRaises(TaskValidationError) as ctx:
            await self.service.create_agent_task(
                task_type="research",
                title="   ",
                assigned_by="agent:bernie",
            )
        self.assertIn("title is required", str(ctx.exception).lower())

    async def test_invalid_task_type_rejected(self):
        with self.assertRaises(TaskValidationError) as ctx:
            await self.service.create_agent_task(
                task_type="system",
                title="x",
                assigned_to="agent:bernie",
                assigned_by="agent:bernie",
            )
        self.assertIn("research|bernie|code", str(ctx.exception))

    async def test_research_task_creation_sets_up_task_row(self):
        t = await self.service.create_agent_task(
            task_type="research",
            title="Find dentist",
            assigned_to="agent:bernie",
            assigned_by="agent:bernie"
        )
        self.assertEqual(t["type"], "research")
        self.assertEqual(t["title"], "Find dentist")
        self.assertEqual(t["assigned_to"], "agent:bernie")

        self.mock_enqueue.assert_called_once_with(
            t["id"], "Find dentist", actor_id="agent:bernie",
        )

    async def test_visibility_rules(self):
        # Code task type gets visibility: internal
        t1 = await self.service.create_agent_task(
            task_type="code",
            title="Refactor API",
            assigned_to="agent:nanobot",
            assigned_by="agent:bernie"
        )
        self.assertEqual(t1["visibility"], "internal")

        # Bernie task type gets visibility: family
        t2 = await self.service.create_agent_task(
            task_type="bernie",
            title="Plan dinner",
            assigned_to="agent:bernie",
            assigned_by="agent:bernie"
        )
        self.assertEqual(t2["visibility"], "family")

    async def test_agent_complete_records_execution(self):
        # Create an agent task first
        t = await self.service.create_agent_task(
            task_type="research",
            title="Dentist search",
            assigned_to="agent:bernie",
            assigned_by="agent:bernie"
        )
        self.store.tasks[t["id"]]["status"] = "todo"
        self.store.tasks[t["id"]]["kanban_status"] = "todo"

        # Complete via kanban
        updated = await self.service.complete_task(
            t["id"],
            actor_id="agent:bernie",
            note="Completed dentist find",
            via="kanban"
        )
        self.assertEqual(updated["kanban_status"], "done")
        self.assertEqual(updated["status"], "done")

        # Verify run execution was recorded in store
        executions = await self.store.list_executions(t["id"])
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0]["status"], "completed")
        self.assertEqual(executions[0]["logs"], "Completed dentist find")

    async def test_chore_complete_approval_dm(self):
        # Setup mock notification_router
        mock_router = MagicMock()
        mock_router.notification = MagicMock(side_effect=lambda **kw: MagicMock(**kw, id=1010))
        mock_router.notify = AsyncMock(return_value={"discord": MagicMock(id=1010)})
        self.service.notification_router = mock_router

        # Setup mock resolver to resolve assigner and their discord id
        self.mock_registry.resolve.side_effect = lambda name: "red" if name == "person:red" else name
        self.mock_registry.get.side_effect = lambda uid: {"discord_id": "9999"} if uid == "red" else None

        # Create a chore that requires approval
        chore = await self.store.create_task(
            title="Clean bedroom",
            assigned_to="person:child2",
            assigned_by="person:red",
            requires_approval=True
        )

        updated = await self.service.complete_task(
            chore["id"],
            actor_id="person:child2",
            note="All clean",
            via="api"
        )
        self.assertEqual(updated["kanban_status"], "done")
        # InInMemoryTaskStore approve_task sets status to approved, but here since requires_approval is True,
        # it remains done but not approved, status is "done".
        self.assertNotEqual(updated.get("status"), "approved")

        # Verify notification DM was fired
        mock_router.notify.assert_called_once()
        self.assertIn("Clean bedroom", mock_router.notification.call_args[1]["message"])

    async def test_chore_complete_auto_approve(self):
        # Create a chore that does NOT require approval
        chore = await self.store.create_task(
            title="Take out trash",
            assigned_to="person:child2",
            assigned_by="person:red",
            requires_approval=False
        )

        updated = await self.service.complete_task(
            chore["id"],
            actor_id="person:child2",
            note="Done",
            via="api"
        )
        # Should be auto-approved
        self.assertEqual(updated["status"], "approved")

    async def test_block_sends_ping(self):
        # Setup mock notification_router
        mock_router = MagicMock()
        mock_router.notification = MagicMock(side_effect=lambda **kw: MagicMock(**kw))
        mock_router.notify = AsyncMock(return_value={})
        self.service.notification_router = mock_router

        # Setup config anvil channel
        self.service.config["anvil_channel_id"] = 8888

        # Create a task
        t = await self.service.create_agent_task(
            task_type="research",
            title="Dentist finder",
            assigned_to="agent:bernie",
            assigned_by="agent:bernie"
        )

        updated = await self.service.move_task(
            t["id"],
            "blocked",
            actor_id="agent:bernie",
            reason="Login page locked"
        )
        self.assertEqual(updated["kanban_status"], "blocked")
        self.assertEqual(updated.get("error"), "Login page locked")

        # Verify anvil channel notified because no human ping recipient resolved for agent task
        mock_router.notify.assert_called_once()
        self.assertIn("blocked: Login page locked", mock_router.notification.call_args[1]["message"])

    async def test_agent_complete_via_board_records_execution(self):
        t = await self.service.create_agent_task(
            task_type="research",
            title="Board complete",
            assigned_to="agent:bernie",
            assigned_by="agent:bernie",
        )
        self.store.tasks[t["id"]]["status"] = "running"
        self.store.tasks[t["id"]]["kanban_status"] = "running"

        updated = await self.service.move_task(
            t["id"], "done", actor_id="agent:bernie", via="board",
        )
        self.assertEqual(updated["kanban_status"], "done")
        executions = await self.store.list_executions(t["id"])
        self.assertEqual(len(executions), 1)
        self.assertTrue(executions[0]["run_id"].startswith("board-"))

    async def test_create_chore_task_valid(self):
        # Setup registry mock
        self.mock_registry.resolve.side_effect = lambda name: name.lower().replace("person:", "") if name else None
        self.mock_registry.get.side_effect = lambda uid: {"role": "parents", "discord_id": "1111"} if uid == "mom" else {"role": "kids", "discord_id": "2222"}

        # Self-assigned by parent
        t = await self.service.create_chore_task(
            title="Clean living room",
            assigned_to="person:mom",
            assigned_by="person:mom",
            priority="high",
        )
        self.assertEqual(t["title"], "Clean living room")
        self.assertEqual(t["assigned_to"], "mom")
        self.assertEqual(t["requires_approval"], False)
        self.assertEqual(t["priority"], "high")

    async def test_create_chore_task_disallowed_assignee(self):
        # Assignee not in registry
        self.mock_registry.get.side_effect = lambda uid: None
        with self.assertRaises(TaskValidationError) as ctx:
            await self.service.create_chore_task(
                title="Feed dogs",
                assigned_to="person:unknown",
                assigned_by="person:mom",
            )
        self.assertIn("assigned person not found", str(ctx.exception).lower())

    async def test_create_chore_task_approval_flag(self):
        # Setup roles: mom is a parent, child2 is a kid
        self.mock_registry.resolve.side_effect = lambda name: name.lower().replace("person:", "") if name else None
        self.mock_registry.get.side_effect = lambda uid: {"role": "parents"} if uid == "mom" else {"role": "kids"}

        t = await self.service.create_chore_task(
            title="Do dishes",
            assigned_to="person:child2",
            assigned_by="person:mom",
        )
        self.assertEqual(t["requires_approval"], True)
        self.assertEqual(t["approver_person_id"], "mom")

    async def test_finalize_research_task_ok(self):
        # Setup research task
        t = await self.store.create_agent_task(
            type="research",
            title="Search schools",
            details="",
            assigned_to="agent:bernie",
            assigned_by="person:mom",
            priority="normal",
            horizon=None,
            visibility="family",
        )
        self.store.tasks[t["id"]]["status"] = "running"

        # Finalize as ok
        await self.service.finalize_research_task(
            t["id"],
            ok=True,
            summary="Found 3 schools",
            run_id="ct-100",
        )

        task = await self.store.get_task(t["id"])
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["completion_note"], "Found 3 schools")

    async def test_reassign_normalizes_person_id_like_create_chore(self):
        self.mock_registry.resolve.side_effect = lambda name: name.lower().replace("person:", "") if name else None
        self.mock_registry.get.side_effect = lambda uid: {"role": "kids"} if uid == "child2" else {"role": "parents"}

        chore = await self.service.create_chore_task(
            title="Dishes",
            assigned_to="person:mom",
            assigned_by="person:mom",
        )
        updated = await self.service.reassign_task(
            chore["id"],
            actor_id="person:mom",
            assigned_to="child2",
        )
        self.assertEqual(updated["assigned_to"], "child2")

    async def test_reassign_agent_task_uses_person_prefix(self):
        """Agent-task rows store family assignees as person:{id} (matches create_agent_task)."""
        self.mock_registry.resolve.side_effect = lambda name: name.lower().replace("person:", "") if name else None
        self.service.config = {
            "task_types": {
                **_TASK_TYPES,
                "research": ["agent:bernie", "person:child2"],
            }
        }
        t = await self.service.create_agent_task(
            task_type="research",
            title="Topic",
            assigned_to="agent:bernie",
            assigned_by="agent:bernie",
        )
        updated = await self.service.reassign_task(
            t["id"],
            actor_id="agent:bernie",
            assigned_to="child2",
        )
        self.assertEqual(updated["assigned_to"], "person:child2")

    async def test_snooze_task_pending_only(self):
        chore = await self.store.create_task(
            title="Walk dog",
            assigned_to="child2",
            assigned_by="mom",
        )
        updated = await self.service.snooze_task(
            chore["id"],
            actor_id="child2",
            snooze_until="2026-06-02T08:00:00",
        )
        self.assertIsNotNone(updated.get("snooze_until"))
        self.store.tasks[chore["id"]]["status"] = "done"
        with self.assertRaises(TaskValidationError):
            await self.service.snooze_task(
                chore["id"],
                actor_id="child2",
                snooze_until="2026-06-03T08:00:00",
            )

    async def test_finalize_research_task_fail(self):
        # Setup research task
        t = await self.store.create_agent_task(
            type="research",
            title="Search schools failed",
            details="",
            assigned_to="agent:bernie",
            assigned_by="person:mom",
            priority="normal",
            horizon=None,
            visibility="family",
        )
        self.store.tasks[t["id"]]["status"] = "running"

        mock_router = MagicMock()
        mock_router.notification = MagicMock(side_effect=lambda **kw: MagicMock(**kw))
        mock_router.notify = AsyncMock(return_value={})
        self.service.notification_router = mock_router

        # Finalize as failed
        await self.service.finalize_research_task(
            t["id"],
            ok=False,
            summary="Failed search",
            run_id="ct-101",
            error="Connection timeout",
        )

        task = await self.store.get_task(t["id"])
        self.assertEqual(task["kanban_status"], "blocked")
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["error"], "Connection timeout")

    async def test_add_comment_requires_text(self):
        chore = await self.store.create_task(title="x", assigned_to="child2", assigned_by="mom")
        with self.assertRaises(TaskValidationError):
            await self.service.add_comment(chore["id"], actor_id="mom", text="  ")

    async def test_add_comment_records_event(self):
        chore = await self.store.create_task(title="x", assigned_to="child2", assigned_by="mom")
        await self.service.add_comment(chore["id"], actor_id="mom", text="looks good")
        events = await self.store.list_task_events(chore["id"])
        self.assertTrue(any(e.get("type") == "comment" for e in events))

    async def test_delete_task_removes_row(self):
        chore = await self.store.create_task(title="gone", assigned_to="child2", assigned_by="mom")
        tid = chore["id"]
        out = await self.service.delete_task(tid, actor_id="mom")
        self.assertTrue(out.get("ok"))
        self.assertIsNone(await self.store.get_task(tid))

    async def test_update_task_merges_reassign_and_fields(self):
        chore = await self.store.create_task(title="old", assigned_to="child2", assigned_by="mom")
        updated = await self.service.update_task(
            chore["id"],
            actor_id="mom",
            updates={"title": "new", "assigned_to": "dad"},
        )
        self.assertEqual(updated["title"], "new")
        self.assertEqual(updated["assigned_to"], "dad")

    async def test_decline_task_deletes_and_notifies(self):
        mock_router = MagicMock()
        mock_router.notification = MagicMock(side_effect=lambda **kw: MagicMock(**kw))
        mock_router.notify = AsyncMock(return_value={})
        self.service.notification_router = mock_router
        self.mock_registry.display_name = MagicMock(return_value="Child2")

        chore = await self.store.create_task(title="nope", assigned_to="child2", assigned_by="dad")
        with patch("task_access.person_to_discord_id", return_value="999"):
            out = await self.service.decline_task(chore["id"], actor_id="child2", reason="too busy")
        self.assertEqual(out.get("task_id"), chore["id"])
        self.assertIsNone(await self.store.get_task(chore["id"]))
        mock_router.notify.assert_called()

    async def test_report_task_not_completed_keeps_task(self):
        mock_router = MagicMock()
        mock_router.notification = MagicMock(side_effect=lambda **kw: MagicMock(**kw))
        mock_router.notify = AsyncMock(return_value={})
        self.service.notification_router = mock_router
        self.mock_registry.display_name = MagicMock(return_value="Child2")

        chore = await self.store.create_task(title="wash", assigned_to="child2", assigned_by="dad")
        with patch("task_access.person_to_discord_id", return_value="999"):
            out = await self.service.report_task_not_completed(
                chore["id"], actor_id="child2", note="ran out of time",
            )
        self.assertEqual(out.get("task_id"), chore["id"])
        self.assertIsNotNone(await self.store.get_task(chore["id"]))
        mock_router.notify.assert_called()


class TestUnifiedTaskServiceIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import database as _db_mod
        from db_binding import bind_database
        bind_database(_db_mod)

        from tools import load_all_domains
        load_all_domains()
        self.store = InMemoryTaskStore()

        self.enqueue_patcher = patch.object(
            UnifiedTaskService, "enqueue_research_run", new_callable=AsyncMock,
        )
        self.mock_enqueue = self.enqueue_patcher.start()

        # Configure real or mock person_registry
        from constants import registry as person_registry
        person_registry.load({"family_members": {"bernie": {"discord_id": "123", "role": "admin"}}})

        self.service = UnifiedTaskService(
            task_store=self.store,
            person_registry=person_registry,
            config={"task_types": _TASK_TYPES}
        )

    async def asyncTearDown(self):
        self.enqueue_patcher.stop()

    async def test_kanban_create_and_api_post_share_facade(self):
        # 1. Kanban Tool path
        class ToolCtx:
            shadow = False
            person_id = "agent:bernie"
            config = {"task_types": _TASK_TYPES}
            services = ServiceRefs(task_store=self.store, unified_tasks=self.service)

        from tools.kanban import handle_kanban_create

        # Test creation via kanban tool
        tool_out = await handle_kanban_create(
            {"type": "research", "title": "Tool Dentist Find", "assigned_to": "agent:bernie"},
            ToolCtx()
        )
        self.assertIn("created", tool_out)

        # Verify the created task exists in store
        tasks = list(self.store.tasks.values())
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "Tool Dentist Find")

        # 2. API path delegation
        from fastapi.testclient import TestClient
        from api import create_api

        container = ServiceContainer(
            task_store=self.store,
            unified_tasks=self.service
        )
        app = create_api(None, container)
        client = TestClient(app)

        # Mock the FastAPI depends token verification
        from api import verify_token
        app.dependency_overrides[verify_token] = lambda: MagicMock(id="agent:bernie", role="admin")

        response = client.post(
            "/api/tasks/agent",
            json={"type": "research", "title": "API Dentist Find", "assigned_to": "agent:bernie"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["title"], "API Dentist Find")

        # Total tasks in store should be 2, showing both entry points successfully share the facade and same store!
        all_tasks = list(self.store.tasks.values())
        self.assertEqual(len(all_tasks), 2)

    async def test_kanban_vs_api_move_equivalence(self):
        # Create two identical tasks to compare
        t1 = await self.service.create_agent_task(
            task_type="research",
            title="Find dentist 1",
            assigned_to="agent:bernie",
            assigned_by="agent:bernie"
        )
        t2 = await self.service.create_agent_task(
            task_type="research",
            title="Find dentist 2",
            assigned_to="agent:bernie",
            assigned_by="agent:bernie"
        )

        class ToolCtx:
            shadow = False
            person_id = "agent:bernie"
            config = {"task_types": _TASK_TYPES}
            services = ServiceRefs(task_store=self.store, unified_tasks=self.service)
            task_id = t1["id"]

        from tools.kanban import handle_kanban_complete
        from fastapi.testclient import TestClient
        from api import create_api

        # 1. Complete task 1 via Kanban tool
        await handle_kanban_complete({"summary": "done first"}, ToolCtx())

        # 2. Complete task 2 via API move POST route
        container = ServiceContainer(
            task_store=self.store,
            unified_tasks=self.service
        )
        container.connection_manager = MagicMock()
        container.connection_manager.broadcast = AsyncMock()
        app = create_api(None, container)
        client = TestClient(app)

        from api import verify_token
        app.dependency_overrides[verify_token] = lambda: MagicMock(id="agent:bernie", role="admin")

        response = client.post(
            f"/api/tasks/{t2['id']}/move",
            json={"status": "done"}
        )
        self.assertEqual(response.status_code, 200)

        # Assert identical store state for both completed tasks!
        task1 = await self.store.get_task(t1["id"])
        task2 = await self.store.get_task(t2["id"])

        self.assertEqual(task1["kanban_status"], "done")
        self.assertEqual(task2["kanban_status"], "done")
        self.assertEqual(task1["status"], task2["status"])
        self.assertEqual(len(await self.store.list_executions(t1["id"])), 1)
        self.assertEqual(len(await self.store.list_executions(t2["id"])), 1)

    async def test_tool_vs_api_chore_creation_equivalence(self):
        # Ensure we have our users in person_registry
        from constants import registry as person_registry
        person_registry.load({
            "family_members": {
                "mom": {"discord_id": "1111", "role": "parents"},
                "child2": {"discord_id": "2222", "role": "kids"}
            }
        })

        # 1. Create via Kanban/Tasks Tool
        class ToolCtx:
            shadow = False
            person_id = "mom"
            config = {"task_types": _TASK_TYPES}
            services = ServiceRefs(task_store=self.store, unified_tasks=self.service)

        from tools.tasks import handle_create_task
        tool_out = await handle_create_task(
            {"title": "Clean kitchen", "assigned_to": "child2", "priority": "high"},
            ToolCtx()
        )
        self.assertIn("created", tool_out.lower())

        # Verify task in store
        all_tasks = list(self.store.tasks.values())
        # First task is from tool
        task_from_tool = [t for t in all_tasks if t["title"] == "Clean kitchen"][0]
        self.assertEqual(task_from_tool["assigned_to"], "child2")
        self.assertEqual(task_from_tool["assigned_by"], "mom")
        self.assertEqual(task_from_tool["requires_approval"], True)
        self.assertEqual(task_from_tool["priority"], "high")

        # 2. Create via API POST /api/tasks
        from fastapi.testclient import TestClient
        from api import create_api

        container = ServiceContainer(
            task_store=self.store,
            unified_tasks=self.service
        )
        container.connection_manager = MagicMock()
        container.connection_manager.broadcast = AsyncMock()
        app = create_api(None, container)
        client = TestClient(app)

        from api import verify_token
        app.dependency_overrides[verify_token] = lambda: MagicMock(id="person:mom", role="parents")

        response = client.post(
            "/api/tasks",
            json={"title": "Clean bathroom", "assigned_to": "child2", "priority": "high"}
        )
        self.assertEqual(response.status_code, 200)
        task_from_api = response.json()

        # Both tasks should have equivalent metadata structure
        self.assertEqual(task_from_api["title"], "Clean bathroom")
        self.assertEqual(task_from_api["assigned_to"], "child2")
        self.assertEqual(task_from_api["assigned_by"], "mom")
        self.assertEqual(task_from_api["requires_approval"], True)
        self.assertEqual(task_from_api["priority"], "high")
