# tests/test_task_status.py
import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from task_status import to_unified_status, to_legacy_status, due_to_horizon, legacy_status_to_unified

class TestStatusMapping(unittest.TestCase):
    def test_pending_not_started_maps_to_todo(self):
        self.assertEqual(to_unified_status("pending", in_progress=False), "todo")
    def test_pending_in_progress_maps_to_running(self):
        self.assertEqual(to_unified_status("pending", in_progress=True), "running")
    def test_done_and_approved_map_to_done(self):
        self.assertEqual(to_unified_status("done", False), "done")
        self.assertEqual(to_unified_status("approved", False), "done")
    def test_todo_back_to_pending_not_started(self):
        self.assertEqual(to_legacy_status("todo"), ("pending", False))
    def test_running_back_to_pending_in_progress(self):
        self.assertEqual(to_legacy_status("running"), ("pending", True))
    def test_ready_blocked_triage_present_as_pending(self):
        self.assertEqual(to_legacy_status("ready"), ("pending", False))
        self.assertEqual(to_legacy_status("blocked"), ("pending", False))
        self.assertEqual(to_legacy_status("triage"), ("pending", False))   # Suggestion 6
    def test_done_and_archived_back_to_done(self):
        self.assertEqual(to_legacy_status("done"), ("done", False))
        self.assertEqual(to_legacy_status("archived"), ("done", False))    # Suggestion 6
    def test_horizon_from_due_iso(self):
        self.assertEqual(due_to_horizon("2026-05-17T14:30:00Z"), "2026-05")
    def test_horizon_someday_when_no_due(self):
        self.assertEqual(due_to_horizon(None), "someday")
        self.assertEqual(due_to_horizon(""), "someday")
    def test_legacy_filter_to_unified_lanes(self):
        self.assertEqual(legacy_status_to_unified("pending"), ("triage","todo","ready","running","blocked"))
        self.assertEqual(legacy_status_to_unified("done"), ("done",))
        self.assertEqual(legacy_status_to_unified("approved"), ("done",))
        self.assertEqual(legacy_status_to_unified("all"), ())
