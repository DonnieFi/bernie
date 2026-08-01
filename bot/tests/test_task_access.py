import sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from task_access import can_view_task, person_id_db_forms, person_matches, registry_person_id, person_to_discord_id


class PersonMatches(unittest.TestCase):
    def test_bare_and_prefixed_equivalent(self):
        self.assertTrue(person_matches("mom", "mom"))
        self.assertTrue(person_matches("person:mom", "mom"))
        self.assertTrue(person_matches("mom", "person:mom"))

    def test_case_insensitive(self):
        self.assertTrue(person_matches("person:Mom", "mom"))
        self.assertTrue(person_matches("Dad", "dad"))

    def test_unrelated(self):
        self.assertFalse(person_matches("dad", "mom"))


class RegistryPersonId(unittest.TestCase):
    def test_strips_person_prefix(self):
        self.assertEqual(registry_person_id("person:child2"), "child2")

    def test_bare_lowercased(self):
        self.assertEqual(registry_person_id("Mom"), "mom")

    def test_agent_unchanged(self):
        self.assertEqual(registry_person_id("agent:bernie"), "agent:bernie")

    def test_none(self):
        self.assertIsNone(registry_person_id(None))


class PersonIdDbForms(unittest.TestCase):
    def test_both_storage_shapes(self):
        self.assertEqual(person_id_db_forms("Mom"), ("mom", "person:mom"))

    def test_agent_unchanged(self):
        self.assertEqual(person_id_db_forms("agent:bernie"), ("agent:bernie", "agent:bernie"))


class CanViewTask(unittest.TestCase):
    def _task(self, **kw):
        base = {"visibility": "family", "assigned_to": "child2", "assigned_by": "mom",
                "acceptable_assignees": ["child2"]}
        base.update(kw)
        return base

    def test_parents_see_all_family(self):
        self.assertTrue(can_view_task(self._task(), "child1", "parents"))

    def test_internal_hidden_from_kids(self):
        t = self._task(visibility="internal", assigned_to="agent:nanobot")
        self.assertFalse(can_view_task(t, "child2", "kids"))

    def test_parents_see_internal(self):
        t = self._task(visibility="internal", assigned_to="agent:nanobot")
        self.assertTrue(can_view_task(t, "dad", "parents"))

    def test_assignee_sees_own(self):
        self.assertTrue(can_view_task(self._task(), "child2", "kids"))

    def test_assigner_sees_child_task(self):
        self.assertTrue(can_view_task(self._task(), "mom", "parents"))

    def test_unrelated_kid_denied(self):
        self.assertFalse(can_view_task(
            self._task(assigned_to="child1", acceptable_assignees=["child1"]), "child2", "kids"))

    def test_claimable_assignee_allowed(self):
        t = self._task(assigned_to=None, acceptable_assignees=["child2", "child1"])
        self.assertTrue(can_view_task(t, "child2", "kids"))


class PersonToDiscordId(unittest.TestCase):
    def test_resolves_discord_id(self):
        from constants import registry
        registry.load({
            "family_members": {
                "Dad": {"canonical_id": "dad", "discord_id": 123456789012345678}
            }
        })
        self.assertEqual(person_to_discord_id("dad"), 123456789012345678)
        self.assertEqual(person_to_discord_id("person:dad"), 123456789012345678)
        self.assertIsNone(person_to_discord_id("unknown"))

    def test_resolves_alias(self):
        from constants import registry
        registry.load({
            "family_members": {
                "Dad": {
                    "canonical_id": "dad",
                    "discord_id": 123456789012345678,
                    "aliases": ["red"],
                }
            }
        })
        self.assertEqual(person_to_discord_id("red"), 123456789012345678)
