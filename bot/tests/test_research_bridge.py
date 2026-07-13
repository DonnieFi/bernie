import sys, os, tempfile, unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database as db
from research_bridge import finalize_unified_from_research  # the write-back helper (Step 3)

class BridgeWriteBack(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self._tmp.close()
        db.DB_PATH = self._tmp.name; await db.init_db()
        self.tid = (await db.create_agent_task(type="research", title="dentist", assigned_by="agent:bernie",
                  assigned_to="agent:bernie", status="running"))["id"]
    async def asyncTearDown(self):
        os.unlink(self._tmp.name)
    async def test_success_marks_done_and_records_run(self):
        await finalize_unified_from_research(self.tid, ok=True, summary="3 options", run_id="ct-1", metrics={"tokens_in": 1200})
        t = await db.get_task(self.tid); self.assertEqual(t["kanban_status"], "done")
        self.assertEqual(t["completion_note"], "3 options")
        runs = await db.list_executions(self.tid); self.assertEqual(runs[0]["status"], "completed")
    async def test_failure_marks_blocked_with_logs(self):
        await finalize_unified_from_research(self.tid, ok=False, summary="timeout", run_id="ct-2", error="TimeoutError", logs="trace…")
        t = await db.get_task(self.tid); self.assertEqual(t["kanban_status"], "blocked")
        runs = await db.list_executions(self.tid); self.assertEqual(runs[0]["status"], "crashed")

    async def test_board_delivery_archives_html_and_emails(self):
        """deliver=True writes a local HTML archive and records a 'delivered' event (email mocked)."""
        import os, glob
        from research_executive_delivery import ResearchDeliveryPlan

        sent = {}

        async def _fake_gateway(**kwargs):
            sent.update(
                to=kwargs.get("to"),
                subject=kwargs.get("subject"),
                body=kwargs.get("body"),
                cc=kwargs.get("cc"),
            )
            return "mock-msg-id"

        plan = ResearchDeliveryPlan(
            should_deliver=True,
            route="suggest",
            body_text="# Hotels\n- one\n- two",
            prefix="",
            urgency="normal",
            fallback=False,
            topic="dentist",
            deliverable=MagicMock(),
        )
        with patch(
            "research_executive_delivery.prepare_research_for_delivery",
            AsyncMock(return_value=plan),
        ), patch("delivery_gateway.send_email_via_gateway", side_effect=_fake_gateway):
            await finalize_unified_from_research(
                self.tid, ok=True, summary="# Hotels\n- one\n- two", run_id="ct-3", deliver=True
            )

        d = os.path.join(os.path.dirname(db.DB_PATH), "research")
        files = glob.glob(os.path.join(d, f"research-{self.tid}-*.html"))
        self.addCleanup(lambda: [os.remove(p) for p in files])
        self.assertTrue(files, "HTML archive was not written")
        with open(files[0], encoding="utf-8") as f:
            doc = f.read()
        self.assertIn("Hotels", doc)
        self.assertIn("<ul>", doc)
        self.assertIn("<li>one</li>", doc)
        self.assertIn("</ul>", doc)

        self.assertEqual(sent.get("subject"), "Research: dentist")   # title from asyncSetUp
        evs = await db.list_task_events(self.tid)
        self.assertTrue([e for e in evs if e["event_type"] == "delivered"], "no 'delivered' event recorded")
