"""Tests for bot/agent_utils.py — parse_typed + validation_error_summary.

Phase 28.5 §2 foundation: validates the Pydantic parser used by all Ollama
workers replacing their ad-hoc _parse_output / _safe_json_array helpers.
"""
import unittest

from agent_utils import parse_typed, validation_error_summary
from typed_outputs import ReflectionNotes, ResearchQueries, ConsolidationOutput


class TestParseTypedReflection(unittest.TestCase):
    def test_clean_json(self):
        raw = '{"household_summary":"Quiet day","per_person":{"child1":"recital prep"},"confidence":0.78}'
        n = parse_typed(raw, ReflectionNotes)
        self.assertIsNotNone(n)
        self.assertAlmostEqual(n.confidence, 0.78)
        self.assertEqual(n.per_person["child1"], "recital prep")

    def test_markdown_fence_stripped(self):
        # Common Ollama failure mode: wraps response in ```json fences despite
        # the system prompt forbidding them.
        raw = '```json\n{"household_summary":"X","per_person":{},"confidence":0.5}\n```'
        n = parse_typed(raw, ReflectionNotes)
        self.assertIsNotNone(n)
        self.assertEqual(n.household_summary, "X")

    def test_chatty_preamble_extracted(self):
        # Model includes commentary before the JSON. The balanced-brace scan
        # finds the outermost {...} and validates that.
        raw = (
            "Here's the reflection you asked for:\n\n"
            '{"household_summary":"Calm","per_person":{},"confidence":0.6}\n\n'
            "Hope that helps!"
        )
        n = parse_typed(raw, ReflectionNotes)
        self.assertIsNotNone(n)
        self.assertEqual(n.household_summary, "Calm")

    def test_garbage_returns_none(self):
        # No retry / no fence — caller decides what to do with None.
        n = parse_typed("not json at all just prose", ReflectionNotes)
        self.assertIsNone(n)

    def test_empty_returns_none(self):
        self.assertIsNone(parse_typed("", ReflectionNotes))
        self.assertIsNone(parse_typed(None, ReflectionNotes))

    def test_field_violation_returns_none(self):
        # Confidence > 1 fails validation; parser tries all candidates then
        # returns None rather than silently emitting a bad model.
        raw = '{"household_summary":"x","per_person":{},"confidence":1.5}'
        self.assertIsNone(parse_typed(raw, ReflectionNotes))

    def test_per_person_length_violation_returns_none(self):
        # Per-value length is enforced via Annotated[str, Field(max_length=200)]
        # in typed_outputs.ReflectionNotes — guards plan-review W1.
        long_val = "x" * 201
        raw = '{"household_summary":"x","per_person":{"a":"' + long_val + '"},"confidence":0.5}'
        self.assertIsNone(parse_typed(raw, ReflectionNotes))


class TestParseTypedListLike(unittest.TestCase):
    def test_research_queries_object_wrapper(self):
        raw = '{"queries":["lisbon","portugal beaches"]}'
        q = parse_typed(raw, ResearchQueries)
        self.assertIsNotNone(q)
        self.assertEqual(q.queries, ["lisbon", "portugal beaches"])

    def test_research_queries_chatty_wrapper(self):
        raw = 'Sure! Here are queries:\n{"queries":["a","b"]}\nDone.'
        q = parse_typed(raw, ResearchQueries)
        self.assertIsNotNone(q)
        self.assertEqual(q.queries, ["a", "b"])

    def test_research_queries_too_many_returns_none(self):
        raw = '{"queries":["a","b","c","d"]}'
        self.assertIsNone(parse_typed(raw, ResearchQueries))


class TestParseTypedConsolidation(unittest.TestCase):
    def test_full_structure(self):
        raw = (
            '{"new_routines":[{"name":"Mon piano","pattern":{},"confidence":0.8}],'
            '"reinforced":[{"name":"Wed soccer","confidence_bump":0.1}],'
            '"preference_updates":[{"key":"wake_time","value":"6:30","confidence":0.9}],'
            '"observations":[{"text":"Likes pour-over","confidence":0.85}]}'
        )
        c = parse_typed(raw, ConsolidationOutput)
        self.assertIsNotNone(c)
        self.assertEqual(len(c.new_routines), 1)
        self.assertEqual(c.new_routines[0].confidence, 0.8)

    def test_empty_object_uses_defaults(self):
        c = parse_typed('{}', ConsolidationOutput)
        self.assertIsNotNone(c)
        self.assertEqual(c.new_routines, [])
        self.assertEqual(c.reinforced, [])

    def test_nested_field_violation_returns_none(self):
        # Bad confidence in a nested routine — top-level parse must reject.
        raw = '{"new_routines":[{"name":"x","pattern":{},"confidence":2.5}]}'
        self.assertIsNone(parse_typed(raw, ConsolidationOutput))


class TestValidationErrorSummary(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(validation_error_summary("", ReflectionNotes), "no output")
        self.assertEqual(validation_error_summary(None, ReflectionNotes), "no output")

    def test_pydantic_error_format(self):
        raw = '{"household_summary":"x","per_person":{},"confidence":1.5}'
        msg = validation_error_summary(raw, ReflectionNotes)
        # Loc + msg shape, not the full Pydantic dump.
        self.assertIn("confidence", msg)
        self.assertLess(len(msg), 301)

    def test_invalid_json_format(self):
        # Triggers ValueError path inside model_validate_json.
        msg = validation_error_summary("not json", ReflectionNotes)
        self.assertTrue(msg)
        self.assertLess(len(msg), 301)


if __name__ == "__main__":
    unittest.main()
