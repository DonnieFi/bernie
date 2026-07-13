"""Tests for bot/typed_outputs.py — typed worker output models.

Phase 28.5 §1 acceptance: pure model validation. No worker imports yet —
this module is foundation for §2-§4 worker conversions.
"""
import unittest

from pydantic import ValidationError

from typed_outputs import (
    ConsolidationOutput,
    Observation,
    PreferenceUpdate,
    ReflectionNotes,
    ResearchQueries,
    RoutineProposal,
    RoutineReinforcement,
)


class TestReflectionNotes(unittest.TestCase):
    def test_valid(self):
        n = ReflectionNotes(
            household_summary="Quiet day.",
            per_person={"dad": "Watching the network."},
            confidence=0.7,
        )
        self.assertEqual(n.confidence, 0.7)
        self.assertEqual(n.per_person["dad"], "Watching the network.")

    def test_summary_max_length_rejects(self):
        with self.assertRaises(ValidationError):
            ReflectionNotes(
                household_summary="x" * 301,
                per_person={},
                confidence=0.5,
            )

    def test_per_person_value_max_length_rejects(self):
        # Guards the W1 fix from plan review: dict value type uses
        # Annotated[str, Field(max_length=200)] so each per-person string
        # is individually validated. Plain dict[str, str] would not catch this.
        with self.assertRaises(ValidationError):
            ReflectionNotes(
                household_summary="ok",
                per_person={"dad": "x" * 201},
                confidence=0.5,
            )

    def test_confidence_out_of_range_rejects(self):
        with self.assertRaises(ValidationError):
            ReflectionNotes(
                household_summary="ok",
                per_person={},
                confidence=1.5,
            )

    def test_per_person_defaults_to_empty(self):
        n = ReflectionNotes(household_summary="ok", confidence=0.5)
        self.assertEqual(n.per_person, {})


class TestRoutineProposal(unittest.TestCase):
    def test_valid(self):
        r = RoutineProposal(name="bedtime", pattern={"hour": 22}, confidence=0.8)
        self.assertEqual(r.name, "bedtime")
        self.assertEqual(r.pattern["hour"], 22)

    def test_name_max_length_rejects(self):
        with self.assertRaises(ValidationError):
            RoutineProposal(name="x" * 201, pattern={}, confidence=0.5)

    def test_confidence_out_of_range_rejects(self):
        with self.assertRaises(ValidationError):
            RoutineProposal(name="x", pattern={}, confidence=1.1)

    def test_pattern_defaults_to_empty_dict(self):
        r = RoutineProposal(name="x", confidence=0.5)
        self.assertEqual(r.pattern, {})


class TestRoutineReinforcement(unittest.TestCase):
    def test_valid(self):
        r = RoutineReinforcement(name="bedtime", confidence_bump=0.15)
        self.assertAlmostEqual(r.confidence_bump, 0.15)

    def test_confidence_bump_capped_at_0_3(self):
        with self.assertRaises(ValidationError):
            RoutineReinforcement(name="x", confidence_bump=0.5)


class TestPreferenceUpdate(unittest.TestCase):
    def test_valid(self):
        p = PreferenceUpdate(key="wake_time", value="07:00", confidence=0.9)
        self.assertEqual(p.key, "wake_time")

    def test_key_max_length_rejects(self):
        with self.assertRaises(ValidationError):
            PreferenceUpdate(key="x" * 101, value="v", confidence=0.5)


class TestObservation(unittest.TestCase):
    def test_valid_without_expiry(self):
        o = Observation(text="Likes coffee", confidence=0.8)
        self.assertIsNone(o.expires_at)

    def test_valid_with_expiry(self):
        o = Observation(text="x", confidence=0.5, expires_at="2026-12-31")
        self.assertEqual(o.expires_at, "2026-12-31")


class TestConsolidationOutput(unittest.TestCase):
    def test_defaults_to_all_empty_lists(self):
        # An "I have nothing new" pass is valid output.
        c = ConsolidationOutput()
        self.assertEqual(c.new_routines, [])
        self.assertEqual(c.reinforced, [])
        self.assertEqual(c.preference_updates, [])
        self.assertEqual(c.observations, [])

    def test_nested_validation_propagates(self):
        # Confidence > 1 on a nested RoutineProposal must reject at the
        # top-level ConsolidationOutput construction.
        with self.assertRaises(ValidationError):
            ConsolidationOutput(
                new_routines=[{"name": "x", "pattern": {}, "confidence": 1.5}],
            )

    def test_full_structure_validates(self):
        c = ConsolidationOutput(
            new_routines=[{"name": "bedtime", "pattern": {"h": 22}, "confidence": 0.8}],
            reinforced=[{"name": "wake", "confidence_bump": 0.1}],
            preference_updates=[{"key": "color", "value": "blue", "confidence": 0.7}],
            observations=[{"text": "calm evenings", "confidence": 0.6}],
        )
        self.assertEqual(len(c.new_routines), 1)
        self.assertEqual(len(c.reinforced), 1)
        self.assertEqual(len(c.preference_updates), 1)
        self.assertEqual(len(c.observations), 1)


class TestResearchQueries(unittest.TestCase):
    def test_valid(self):
        q = ResearchQueries(queries=["lisbon nightlife", "lisbon food"])
        self.assertEqual(len(q.queries), 2)

    def test_max_three_queries(self):
        # Matches the old _safe_json_array _coerce cap of 3.
        with self.assertRaises(ValidationError):
            ResearchQueries(queries=["a", "b", "c", "d"])


if __name__ == "__main__":
    unittest.main()
