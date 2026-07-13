import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from task_types import validate_assignment

# api.py builds its own gating call; here we lock the contract the endpoint must enforce.
CFG = {"task_types": {"chore": ["person:*"], "research": ["agent:bernie", "agent:research-worker"]}}

class ApiGatingContract(unittest.TestCase):
    def test_rejects_chore_assigned_to_agent(self):
        self.assertFalse(validate_assignment("chore", "agent:bernie", CFG))
    def test_allows_chore_assigned_to_person(self):
        self.assertTrue(validate_assignment("chore", "person:child2", CFG))
