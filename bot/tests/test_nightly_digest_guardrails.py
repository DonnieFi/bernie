"""Guardrails for nightly digest insight extraction."""
import unittest

from insight_extraction import (
    DIGEST_SYSTEM,
    build_digest_user_prompt,
    looks_like_one_off_insight,
    parse_insights_from_response,
)


class TestDigestGuardrails(unittest.TestCase):
    def test_system_prompt_forbids_one_offs_and_relative_time(self):
        self.assertIn("one-off", DIGEST_SYSTEM.lower())
        self.assertIn("tonight", DIGEST_SYSTEM.lower())

    def test_user_prompt_includes_extraction_rules(self):
        prompt = build_digest_user_prompt("Child1", "sample conversation")
        self.assertIn("NOT one-off events", prompt)
        self.assertIn("Do not use relative time words", prompt)

    def test_parse_skips_transient_one_off_lines(self):
        text = (
            "- Child1 has band activity tonight\n"
            "- Child1 usually checks homework after dinner\n"
        )
        insights = parse_insights_from_response(text)
        self.assertEqual(len(insights), 1)
        self.assertIn("homework", insights[0]["text"].lower())

    def test_parse_skips_one_off_without_recurring_language(self):
        self.assertTrue(looks_like_one_off_insight("Child1 has a band concert at 7pm"))
        text = "- Child1 has a band concert at 7pm\n- Child1 prefers quiet mornings\n"
        insights = parse_insights_from_response(text)
        self.assertEqual(len(insights), 1)
        self.assertIn("quiet mornings", insights[0]["text"].lower())

    def test_parse_skips_irregular_plural_parties(self):
        self.assertTrue(looks_like_one_off_insight("Child1 has dance parties on Fridays"))
        text = "- Child1 has dance parties\n- Child1 prefers quiet mornings\n"
        insights = parse_insights_from_response(text)
        self.assertEqual(len(insights), 1)
        self.assertIn("quiet mornings", insights[0]["text"].lower())

    def test_parse_keeps_recurring_band_pattern(self):
        text = "- Child1 has band practice every Thursday after school\n"
        insights = parse_insights_from_response(text)
        self.assertEqual(len(insights), 1)
        self.assertTrue(insights[0]["is_permanent"])

    def test_parse_skips_recurring_plus_transient_this_week(self):
        text = "- Child1 has band practice every Thursday this week\n"
        insights = parse_insights_from_response(text)
        self.assertEqual(len(insights), 0)

    def test_transient_words_never_marked_permanent(self):
        text = "- Dad always checks calendar today before leaving\n"
        insights = parse_insights_from_response(text)
        self.assertEqual(len(insights), 0)


class TestConsolidationGuardrails(unittest.TestCase):
    def test_rejects_one_off_concert_routine(self):
        from cognitive_workers.consolidation import _looks_like_one_off_routine
        self.assertTrue(_looks_like_one_off_routine("Child1's band concert"))
        self.assertFalse(_looks_like_one_off_routine("Mon piano", {"day": "mon"}))
        self.assertTrue(_looks_like_one_off_routine("Dietician appointment on Wednesday"))
        self.assertTrue(_looks_like_one_off_routine("Child1 has dance parties"))


if __name__ == "__main__":
    unittest.main()
