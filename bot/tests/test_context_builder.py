"""Integration tests for context_builder (honest, fake I/O per strategist)."""

import unittest
import asyncio
from unittest.mock import patch, AsyncMock

from llm.context_builder import build_context


class FakeCal:
    async def get_todays_events(self):
        await asyncio.sleep(0.01)
        return [{"summary": "test event"}]

    def events_to_text(self, evs):
        return "test event text"


class TestContextBuilder(unittest.IsolatedAsyncioTestCase):
    async def test_gather_only_planned(self):
        # DM no intent -> skip cal
        ctx = await build_context(
            {}, FakeCal(), None,
            user_message="hello",
            channel_id="dm1",
            is_dm=True,
            mode="concierge",
        )
        self.assertIn("presence", ctx)
        self.assertIn("ha_states", ctx)
        self.assertEqual(ctx.get("today_events", ""), "")  # skipped
        self.assertTrue(ctx.get("calendar_lazy"))
        # smithy lazy default -> no calendar in prompt
        ctx2 = await build_context(
            {}, FakeCal(), None,
            user_message="hey",
            channel_id="smithy",
            is_dm=False,
            mode="concierge",
        )
        self.assertEqual(ctx2.get("today_events", ""), "")
        # intent mode + schedule message -> fetch
        cfg_intent = {"context": {"prefetch": {"calendar": "intent"}}}
        ctx3 = await build_context(
            cfg_intent, FakeCal(), None,
            user_message="school today",
            channel_id="smithy",
            is_dm=False,
            mode="concierge",
        )
        self.assertNotEqual(ctx3.get("today_events", ""), "")

    async def test_timing_ms(self):
        with patch("llm.context_builder.db_writes.routed", new_callable=AsyncMock) as mock_routed:
            await build_context(
                {}, FakeCal(), None,
                user_message="hello",
                channel_id="dm1",
                is_dm=True,
                mode="concierge",
            )
            await asyncio.sleep(0.05)
            mock_routed.assert_awaited()
            self.assertEqual(mock_routed.await_args.args[0], "log_context_build")
            kwargs = mock_routed.await_args.kwargs
            self.assertEqual(kwargs.get("calendar_ms"), 0)
            self.assertGreaterEqual(kwargs.get("total_ms", 0), 0)


if __name__ == '__main__':
    unittest.main()
