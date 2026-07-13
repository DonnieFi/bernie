"""Tests for context leg planner (table-driven per strategist)."""

import unittest
from llm.context_legs import should_prefetch_calendar, should_prefetch_weather

_CFG = {
    "furnace_channel_id": 222222222222222222,
    "schedule_channel_id": 111111111111111111,
    "context": {
        "prefetch": {
            "calendar": "lazy",
            "weather": "intent",
        },
    },
}

_CFG_INTENT = {
    **_CFG,
    "context": {
        "prefetch": {
            "calendar": "intent",
            "weather": "intent",
        },
    },
}


class TestContextLegs(unittest.TestCase):
    def test_lazy_dm_no_schedule_skip_calendar(self):
        self.assertFalse(should_prefetch_calendar("dm", True, "concierge", "how are you", _CFG))

    def test_lazy_dm_schedule_still_skips(self):
        self.assertFalse(should_prefetch_calendar("dm", True, "concierge", "what is on today", _CFG))

    def test_intent_dm_schedule_fetch_calendar(self):
        self.assertTrue(should_prefetch_calendar(
            "dm", True, "concierge", "what is on today", _CFG_INTENT,
        ))

    def test_furnace_skip_calendar(self):
        self.assertFalse(should_prefetch_calendar(
            str(_CFG["furnace_channel_id"]), False, "chef", "", _CFG,
        ))

    def test_lazy_smithy_skips_calendar(self):
        self.assertFalse(should_prefetch_calendar(
            str(_CFG["schedule_channel_id"]), False, "concierge", "", _CFG,
        ))

    def test_intent_smithy_schedule_fetch(self):
        self.assertTrue(should_prefetch_calendar(
            str(_CFG_INTENT["schedule_channel_id"]),
            False,
            "concierge",
            "school today",
            _CFG_INTENT,
        ))

    def test_weather_furnace_skip(self):
        self.assertFalse(should_prefetch_weather(
            str(_CFG["furnace_channel_id"]), False, "chef", "", _CFG,
        ))

    def test_weather_smithy_without_keyword_skips(self):
        self.assertFalse(should_prefetch_weather(
            str(_CFG["schedule_channel_id"]), False, "concierge", "hey", _CFG,
        ))

    def test_weather_smithy_with_keyword_fetches(self):
        self.assertTrue(should_prefetch_weather(
            str(_CFG["schedule_channel_id"]), False, "concierge", "rain today?", _CFG,
        ))


if __name__ == '__main__':
    unittest.main()
