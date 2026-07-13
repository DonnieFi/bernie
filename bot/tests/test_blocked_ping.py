import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from notify_targets import blocked_ping_recipient   # new pure helper


class BlockedPing(unittest.TestCase):
    def test_prefers_assigned_by_person(self):
        # plan spec: "person:" prefixed value is returned as-is
        t = {"assigned_by": "person:mom", "assigned_to": "agent:bernie", "title": "x", "id": 5}
        self.assertEqual(blocked_ping_recipient(t), "person:mom")

    def test_falls_back_to_admin_when_agent_assigner(self):
        t = {"assigned_by": "agent:bernie", "assigned_to": "agent:bernie", "title": "x", "id": 5}
        self.assertIsNone(blocked_ping_recipient(t))   # None → caller routes to #anvil

    def test_bare_canonical_person_id_is_treated_as_person(self):
        # canonical convention: persons are bare ("mom"), not "person:mom"
        t = {"assigned_by": "mom", "assigned_to": "agent:bernie", "title": "y", "id": 6}
        self.assertEqual(blocked_ping_recipient(t), "mom")

    def test_dad_bare_canonical_is_treated_as_person(self):
        t = {"assigned_by": "dad", "assigned_to": "agent:research-worker", "title": "z", "id": 7}
        self.assertEqual(blocked_ping_recipient(t), "dad")

    def test_none_assigned_by_returns_none(self):
        t = {"assigned_by": None, "assigned_to": "agent:bernie", "title": "q", "id": 8}
        self.assertIsNone(blocked_ping_recipient(t))

    def test_empty_assigned_by_returns_none(self):
        t = {"assigned_by": "", "assigned_to": "agent:bernie", "title": "q", "id": 9}
        self.assertIsNone(blocked_ping_recipient(t))

    def test_missing_assigned_by_returns_none(self):
        t = {"assigned_to": "agent:bernie", "title": "q", "id": 10}
        self.assertIsNone(blocked_ping_recipient(t))
