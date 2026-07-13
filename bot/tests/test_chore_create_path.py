import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from task_types import validate_assignment

class ChoreCreatePath(unittest.TestCase):
    def setUp(self):
        self.mock_config = {
            "task_types": {
                "chore": ["person:*"],
                "code": ["agent:*"],
                "research": ["agent:research-worker"],
                "bernie": ["agent:bernie"]
            }
        }

    def test_chore_allows_persons(self):
        # real canonical ids are bare; validate_assignment normalizes to person:<name>
        for who in ("mom", "dad", "red", "child2", "child1"):
            self.assertTrue(
                validate_assignment("chore", who, self.mock_config),
                f"chore gating wrongly rejects member {who!r}"
            )

    def test_code_rejects_person(self):
        self.assertFalse(validate_assignment("code", "mom", self.mock_config))

    def test_code_allows_agents(self):
        self.assertTrue(validate_assignment("code", "agent:bernie", self.mock_config))
