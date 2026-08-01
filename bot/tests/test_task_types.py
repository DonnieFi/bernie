# bot/tests/test_task_types.py
import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from task_types import validate_assignment

CFG = {"task_types": {
    "chore": ["person:*"],
    "research": ["agent:bernie", "agent:research-worker"],
    "bernie": ["agent:bernie"],
    "code": ["agent:nanobot"],
}}

class ValidateAssignment(unittest.TestCase):
    def test_chore_glob_matches_any_person(self):
        self.assertTrue(validate_assignment("chore", "person:child2", CFG))
        self.assertTrue(validate_assignment("chore", "person:red", CFG))
    def test_research_exact_allows_bernie_and_worker(self):
        self.assertTrue(validate_assignment("research", "agent:bernie", CFG))
        self.assertTrue(validate_assignment("research", "agent:research-worker", CFG))
    def test_research_rejects_person(self):
        self.assertFalse(validate_assignment("research", "person:child2", CFG))
    def test_code_only_nanobot(self):
        self.assertTrue(validate_assignment("code", "agent:nanobot", CFG))
        self.assertFalse(validate_assignment("code", "agent:bernie", CFG))
    def test_system_never_assignable(self):
        self.assertFalse(validate_assignment("system", "agent:research-worker", CFG))
    def test_unassigned_is_allowed_open_to_claim(self):
        self.assertTrue(validate_assignment("research", None, CFG))
    def test_unknown_type_rejected(self):
        self.assertFalse(validate_assignment("nonsense", "person:child2", CFG))

    def test_bare_canonical_person_id_treated_as_person(self):
        self.assertTrue(validate_assignment("chore", "mom", CFG))   # bare canonical id
        self.assertTrue(validate_assignment("chore", "dad", CFG))

    def test_bare_name_rejected_for_agent_type(self):
        self.assertFalse(validate_assignment("research", "mom", CFG))   # a person can't hold research

    def test_namespaced_agent_still_matches(self):
        self.assertTrue(validate_assignment("research", "agent:bernie", CFG))
