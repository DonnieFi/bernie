"""Unit tests for audit_ungrounded_live_data."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import eval_service
except ModuleNotFoundError:
    eval_service = None


def _ts(minutes_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


class FakeDB:
    """Minimal db_module stub — implements fetch_*_since only (audit public API)."""

    def __init__(self, conversation_rows, tool_rows):
        self.conversation_rows = conversation_rows
        self.tool_rows = tool_rows

    async def fetch_conversation_rows_since(self, since_iso: str) -> list[dict]:
        bound = (since_iso or "").strip()
        return [
            r for r in self.conversation_rows
            if (r.get("created_at") or "") >= bound
        ]

    async def fetch_tool_calls_since(self, since_iso: str) -> list[dict]:
        from database import normalize_since_for_activity_log

        since_norm = normalize_since_for_activity_log(since_iso)
        return [
            r for r in self.tool_rows
            if (r.get("logged_at") or "") >= since_norm
        ]


@unittest.skipUnless(eval_service, "eval_service not available")
class TestAuditUngroundedLiveData(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from eval.audit import _clear_grounding_tools_cache

        _clear_grounding_tools_cache()

    async def test_flags_numeric_claim_without_tool_call(self):
        user_ts = _ts(30)
        asst_ts = _ts(29)
        db = FakeDB(
            conversation_rows=[
                {
                    "id": 1,
                    "channel_id": "123",
                    "role": "user",
                    "content": "lock FamilyCar",
                    "created_at": user_ts,
                },
                {
                    "id": 2,
                    "channel_id": "123",
                    "role": "assistant",
                    "content": "The car is locked at 82% battery with 240 km range.",
                    "created_at": asst_ts,
                },
            ],
            tool_rows=[],
        )
        flags = await eval_service.audit_ungrounded_live_data(db, since_hours=24)
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["reason"], "numeric_claim_without_tool_call")
        self.assertIn("FamilyCar", flags[0]["user_message"])

    async def test_no_flag_when_snapshot_tool_called_nearby(self):
        user_ts = _ts(30)
        asst_ts = _ts(29)
        tool_ts = _ts(28)
        db = FakeDB(
            conversation_rows=[
                {
                    "id": 1,
                    "channel_id": "123",
                    "role": "user",
                    "content": "how did I sleep?",
                    "created_at": user_ts,
                },
                {
                    "id": 2,
                    "channel_id": "123",
                    "role": "assistant",
                    "content": "Sleep score 78, HRV 42 ms.",
                    "created_at": asst_ts,
                },
            ],
            tool_rows=[
                {
                    "logged_at": tool_ts,
                    "description": "Tool <b>get_sleep_summary</b> called via native",
                },
            ],
        )
        flags = await eval_service.audit_ungrounded_live_data(db, since_hours=24)
        self.assertEqual(flags, [])

    async def test_no_flag_without_numeric_claim(self):
        db = FakeDB(
            conversation_rows=[
                {
                    "id": 1,
                    "channel_id": "123",
                    "role": "user",
                    "content": "lock FamilyCar",
                    "created_at": _ts(10),
                },
                {
                    "id": 2,
                    "channel_id": "123",
                    "role": "assistant",
                    "content": "I'll check that for you.",
                    "created_at": _ts(9),
                },
            ],
            tool_rows=[],
        )
        flags = await eval_service.audit_ungrounded_live_data(db, since_hours=24)
        self.assertEqual(flags, [])

    async def test_no_flag_when_transit_read_tool_called(self):
        user_ts = _ts(30)
        asst_ts = _ts(29)
        db = FakeDB(
            conversation_rows=[
                {
                    "id": 1,
                    "channel_id": "123",
                    "role": "user",
                    "content": "/bus route 4",
                    "created_at": user_ts,
                },
                {
                    "id": 2,
                    "channel_id": "123",
                    "role": "assistant",
                    "content": "Bus 3250 moving N at 39 km/h on route 4.",
                    "created_at": asst_ts,
                },
            ],
            tool_rows=[
                {
                    "logged_at": _ts(28),
                    "description": "Tool <b>get_route_buses</b> called via native",
                },
            ],
        )
        flags = await eval_service.audit_ungrounded_live_data(db, since_hours=24)
        self.assertEqual(flags, [])

    async def test_no_flag_for_non_live_data_question(self):
        db = FakeDB(
            conversation_rows=[
                {
                    "id": 1,
                    "channel_id": "123",
                    "role": "user",
                    "content": "what's for dinner?",
                    "created_at": _ts(10),
                },
                {
                    "id": 2,
                    "channel_id": "123",
                    "role": "assistant",
                    "content": "Pasta at 82% deliciousness.",
                    "created_at": _ts(9),
                },
            ],
            tool_rows=[],
        )
        flags = await eval_service.audit_ungrounded_live_data(db, since_hours=24)
        self.assertEqual(flags, [])

    async def test_grounding_tool_names_cached(self):
        from eval.audit import _clear_grounding_tools_cache, _grounding_tool_names

        _clear_grounding_tools_cache()
        _clear_grounding_tools_cache()
        with patch("tools.get_registry", return_value={}), \
             patch("tools.load_all_domains") as mock_load:
            first = _grounding_tool_names()
            second = _grounding_tool_names()
        self.assertIs(first, second)
        mock_load.assert_called_once()
        _clear_grounding_tools_cache()


@unittest.skipUnless(eval_service, "eval_service not available")
class TestFormatUngroundedAuditSection(unittest.TestCase):
    def test_empty_flags_returns_empty_string(self):
        self.assertEqual(eval_service.format_ungrounded_audit_section([]), "")

    def test_renders_summary_lines(self):
        section = eval_service.format_ungrounded_audit_section([
            {
                "created_at": "2026-05-28T11:40:00+00:00",
                "user_message": "lock FamilyCar",
                "response_snippet": "Locked at 82%",
            }
        ])
        self.assertIn("Ungrounded live data", section)
        self.assertIn("lock FamilyCar", section)


class TestActivityLogSinceNormalization(unittest.TestCase):
    def test_normalize_space_format_for_iso_since(self):
        from database import normalize_since_for_activity_log

        self.assertEqual(
            normalize_since_for_activity_log("2026-06-06T02:32:04+00:00"),
            "2026-06-06 02:32:04",
        )

    def test_normalize_z_suffix(self):
        from database import normalize_since_for_activity_log

        self.assertEqual(
            normalize_since_for_activity_log("2026-06-06T02:32:04.123456Z"),
            "2026-06-06 02:32:04",
        )

    def test_normalize_already_space_format(self):
        from database import normalize_since_for_activity_log

        self.assertEqual(
            normalize_since_for_activity_log("2026-06-06 02:32:04"),
            "2026-06-06 02:32:04",
        )

    def test_space_timestamp_passes_sqlite_style_compare(self):
        from database import normalize_since_for_activity_log

        since = normalize_since_for_activity_log("2026-06-06T02:32:04+00:00")
        self.assertGreaterEqual("2026-06-06 15:35:20", since)


if __name__ == "__main__":
    unittest.main()
