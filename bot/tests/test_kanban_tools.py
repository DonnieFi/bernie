import sys, os, unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store.task_store import InMemoryTaskStore

class KanbanCreate(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tools import load_all_domains
        load_all_domains()
        self.store = InMemoryTaskStore()
        class Ctx:
            shadow=False; person_id="agent:bernie"; group="admin"
            config={"task_types": {"research": ["agent:bernie"], "code": ["agent:nanobot"]}}
            class services:
                orchestrator=None
                task_store=self.store
            task_id=None
        self.ctx = Ctx()
        self.ctx.services.task_store = self.store
        from services.unified_task_service import UnifiedTaskService
        self.ctx.services.unified_tasks = UnifiedTaskService(
            task_store=self.store,
            person_registry=None,
            config=self.ctx.config
        )
    async def test_create_rejects_disallowed_assignee(self):
        from tools.kanban import handle_kanban_create
        out = await handle_kanban_create({"type":"research","title":"x","assigned_to":"person:child2"}, self.ctx)
        self.assertIn("not permitted", out.lower())
    async def test_create_rejects_system_type(self):
        from tools.kanban import handle_kanban_create
        out = await handle_kanban_create({"type":"system","title":"x","assigned_to":"agent:bernie"}, self.ctx)
        self.assertIn("research|bernie|code", out.lower())
    async def test_create_research_ok(self):
        from tools.kanban import handle_kanban_create
        with patch("research_bridge.enqueue_for_unified", new_callable=AsyncMock):
            out = await handle_kanban_create(
                {"type": "research", "title": "Find a dentist", "assigned_to": "agent:bernie"},
                self.ctx,
            )
        self.assertIn("#", out)

class KanbanScoped(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tools import load_all_domains; load_all_domains()
        self.store = InMemoryTaskStore()
        t = await self.store.create_agent_task(type="research", title="r", assigned_by="agent:bernie",
                                       assigned_to="agent:bernie", details="", priority="normal", horizon=None, visibility="internal")
        await self.store.set_kanban_status(t["id"], "running")
        class Ctx:
            shadow=False; person_id="agent:bernie"; group="admin"; config={}
            class services:
                orchestrator=None
                task_store=self.store
            task_id=t["id"]
        self.ctx=Ctx(); self.tid=t["id"]
        self.ctx.services.task_store = self.store
        from services.unified_task_service import UnifiedTaskService
        self.ctx.services.unified_tasks = UnifiedTaskService(
            task_store=self.store,
            person_registry=None,
            config=self.ctx.config
        )
    async def test_heartbeat_sets_heartbeat(self):
        from tools.kanban import handle_kanban_heartbeat
        await handle_kanban_heartbeat({"note":"reading clinic pages"}, self.ctx)
        t=await self.store.get_task(self.tid); self.assertIsNotNone(t)
    async def test_complete_sets_done_and_records_run(self):
        from tools.kanban import handle_kanban_complete
        out=await handle_kanban_complete({"summary":"found 3 dentists"}, self.ctx)
        t=await self.store.get_task(self.tid); self.assertEqual(t["kanban_status"],"done")
        self.assertTrue(len(await self.store.list_executions(self.tid))>=1)
    async def test_block_sets_blocked(self):
        from tools.kanban import handle_kanban_block
        await handle_kanban_block({"reason":"PDF login-gated"}, self.ctx)
        self.assertEqual((await self.store.get_task(self.tid))["kanban_status"],"blocked")
    async def test_unbound_ctx_errors(self):
        from tools.kanban import handle_kanban_complete
        from services.unified_task_service import UnifiedTaskService
        class Bare:
            shadow=False; person_id="agent:bernie"; group="admin"; config={}; task_id=None
            class services:
                task_store=None
                unified_tasks=None
        bare_ctx = Bare()
        bare_ctx.services.task_store = self.store
        bare_ctx.services.unified_tasks = UnifiedTaskService(
            task_store=self.store,
            person_registry=None,
            config=bare_ctx.config
        )
        out=await handle_kanban_complete({"summary":"x"}, bare_ctx)
        self.assertIn("no task", out.lower())

class KanbanLink(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tools import load_all_domains; load_all_domains()
        self.store = InMemoryTaskStore()
        a = await self.store.create_agent_task(type="research",title="a",assigned_by="agent:bernie", assigned_to=None, details="", priority="normal", horizon=None, visibility="internal")
        b = await self.store.create_agent_task(type="research",title="b",assigned_by="agent:bernie", assigned_to=None, details="", priority="normal", horizon=None, visibility="internal")
        self.a=a["id"]
        self.b=b["id"]
        class Ctx:
            shadow=False; person_id="agent:bernie"; group="admin"; config={}; task_id=None
            class services:
                task_store=self.store
        self.ctx=Ctx()
        self.ctx.services.task_store = self.store
    async def test_link_then_cycle_rejected(self):
        from tools.kanban import handle_kanban_link
        self.assertIn("linked", (await handle_kanban_link({"parent_id":self.a,"child_id":self.b}, self.ctx)).lower())
        self.assertIn("cycle", (await handle_kanban_link({"parent_id":self.b,"child_id":self.a}, self.ctx)).lower())
