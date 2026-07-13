import asyncio
import inspect
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from api import Person, create_api, verify_token
from service_container import ServiceContainer
from store.task_store import InMemoryTaskStore

_TASK_TYPES = {
    "research": ["agent:bernie"],
    "bernie": ["agent:bernie"],
    "code": ["agent:nanobot"],
}


def _bind_spy(store: InMemoryTaskStore, method_name: str) -> list[str]:
    """Wrap a store method to record invocations while delegating to InMemoryTaskStore."""
    calls: list[str] = []
    original = getattr(InMemoryTaskStore, method_name)

    if inspect.iscoroutinefunction(original):

        async def spy(*args, **kwargs):
            calls.append(method_name)
            return await original(store, *args, **kwargs)

        setattr(store, method_name, spy)
    else:

        def spy(*args, **kwargs):
            calls.append(method_name)
            return original(store, *args, **kwargs)

        setattr(store, method_name, spy)
    return calls


class TestApiTaskStore(unittest.TestCase):
    def setUp(self):
        self.container = ServiceContainer()
        self.store = InMemoryTaskStore()
        self.container.task_store = self.store
        from services.unified_task_service import UnifiedTaskService
        self.container.unified_tasks = UnifiedTaskService(
            task_store=self.store,
            person_registry=None,
            config={"task_types": _TASK_TYPES},
        )
        self.container.connection_manager = MagicMock()
        self.container.connection_manager.broadcast = AsyncMock()
        self.container.notification_orchestrator = MagicMock()
        self.container.notification_orchestrator.notification = MagicMock(
            side_effect=lambda **kw: types.SimpleNamespace(**kw),
        )
        self.container.notification_orchestrator.notify = AsyncMock(return_value={})
        self.app = create_api(None, self.container)
        self.client = TestClient(self.app)
        self._config_patch = patch("api.common.config", {"task_types": _TASK_TYPES})
        self._config_patch.start()

    def tearDown(self):
        self._config_patch.stop()
        self.app.dependency_overrides.clear()

    def _as_user(self, person_id: str, role: str = "admin"):
        self.app.dependency_overrides[verify_token] = lambda: Person(
            id=person_id, role=role, name="Test",
        )

    def test_post_tasks_agent_uses_store_and_enqueue(self):
        self._as_user("person:dad")
        create_calls = _bind_spy(self.store, "create_agent_task")
        get_calls = _bind_spy(self.store, "get_task")
        mock_db = MagicMock()
        mock_db.list_research_memory = AsyncMock(return_value=[])
        mock_db.format_research_memory_for_prompt = MagicMock(return_value="")
        mock_db.create_cognitive_task = AsyncMock(return_value=1)
        with patch("research_bridge.enqueue_for_unified", new_callable=AsyncMock), \
             patch("db_binding.get_database", return_value=mock_db), \
             patch("db_writes.routed", new_callable=AsyncMock, return_value=1) as mock_routed:
            response = self.client.post(
                "/api/tasks/agent",
                json={
                    "type": "research",
                    "title": "Find a dentist",
                    "details": "Look in Seattle",
                    "assigned_to": "agent:bernie",
                    "priority": "high",
                },
                headers={"Authorization": "Bearer testtoken"},
            )
        mock_routed.assert_awaited_once()
        self.assertEqual(mock_routed.await_args.args[0], "create_cognitive_task")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("create_agent_task", create_calls)
        self.assertIn("get_task", get_calls)
        body = response.json()
        self.assertEqual(body["id"], 1)
        self.assertEqual(body["title"], "Find a dentist")

    def test_get_task_detail_reads_from_store(self):
        self._as_user("person:dad")

        async def seed():
            t = await self.store.create_agent_task(
                type="research",
                title="Detail me",
                details="",
                assigned_to="agent:bernie",
                assigned_by="person:dad",
                priority="normal",
                horizon=None,
                visibility="family",
            )
            await self.store.add_task_event(t["id"], "comment", "person:dad", {"text": "hi"})
            return t["id"]

        tid = asyncio.run(seed())
        detail_calls = _bind_spy(self.store, "get_task")
        links_calls = _bind_spy(self.store, "get_task_links")
        exec_calls = _bind_spy(self.store, "list_executions")
        event_calls = _bind_spy(self.store, "list_task_events")

        response = self.client.get(
            f"/api/tasks/{tid}/detail",
            headers={"Authorization": "Bearer testtoken"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["task"]["id"], tid)
        self.assertEqual(len(body["events"]), 1)
        self.assertIn("get_task", detail_calls)
        self.assertIn("get_task_links", links_calls)
        self.assertIn("list_executions", exec_calls)
        self.assertIn("list_task_events", event_calls)

    def test_post_complete_routes_through_store(self):
        self._as_user("person:dad")

        async def seed():
            return await self.store.create_task(
                title="Take out bins",
                assigned_to="person:dad",
                assigned_by="person:mom",
                requires_approval=False,
            )

        task = asyncio.run(seed())
        complete_calls = _bind_spy(self.store, "complete_task")
        event_calls = _bind_spy(self.store, "add_task_event")
        promote_calls = _bind_spy(self.store, "promote_ready_tasks")

        response = self.client.post(
            f"/api/tasks/{task['id']}/complete",
            json={"note": "done"},
            headers={"Authorization": "Bearer testtoken"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("complete_task", complete_calls)
        self.assertIn("add_task_event", event_calls)
        self.assertIn("promote_ready_tasks", promote_calls)

    def test_post_move_running_routes_through_set_kanban_status(self):
        self._as_user("person:dad")

        async def seed():
            t = await self.store.create_agent_task(
                type="research",
                title="Move me",
                details="",
                assigned_to="person:dad",
                assigned_by="person:dad",
                priority="normal",
                horizon=None,
                visibility="family",
            )
            t["status"] = "pending"
            self.store.tasks[t["id"]] = t
            return t

        task = asyncio.run(seed())
        status_calls = _bind_spy(self.store, "set_kanban_status")

        response = self.client.post(
            f"/api/tasks/{task['id']}/move",
            json={"status": "running"},
            headers={"Authorization": "Bearer testtoken"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("set_kanban_status", status_calls)
        updated = asyncio.run(self.store.get_task(task["id"]))
        self.assertEqual(updated["kanban_status"], "running")

    def test_post_approve_routes_through_store(self):
        self._as_user("person:mom", role="parents")

        async def seed():
            t = await self.store.create_task(
                title="Awaiting sign-off",
                assigned_to="person:dad",
                assigned_by="person:mom",
            )
            t["status"] = "done"
            self.store.tasks[t["id"]] = t
            return t

        task = asyncio.run(seed())
        approve_calls = _bind_spy(self.store, "approve_task")
        event_calls = _bind_spy(self.store, "add_task_event")

        response = self.client.post(
            f"/api/tasks/{task['id']}/approve",
            json={"approved": True},
            headers={"Authorization": "Bearer testtoken"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("approve_task", approve_calls)
        self.assertIn("add_task_event", event_calls)


if __name__ == "__main__":
    unittest.main()
