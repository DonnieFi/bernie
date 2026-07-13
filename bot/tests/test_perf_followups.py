"""Tests for perf follow-ups: lazy calendar, intent router, slag funnel, snapshot."""

import unittest
from unittest.mock import AsyncMock, patch

from llm.context_legs import should_prefetch_calendar, looks_schedule_intent, calendar_prefetch_mode
from llm.chat import _resolve_turn_tools
from llm.tool_surface import turn_surface_narrowed
from llm.intent_router import (
    looks_chitchat,
    narrow_tool_domains,
    active_surface_summary,
    _core_domains_for,
)
from llm.slag_funnel import should_suggest_slag, slag_funnel_system_block
from llm.messages import history_verbatim_tail, prune_old_tool_results
from services.live_snapshot import LiveSnapshot, set_live_snapshot, get_live_snapshot


_CFG = {
    "furnace_channel_id": 222222222222222222,
    "schedule_channel_id": 111111111111111111,
    "slag_channel_id": 999999999,
    "context": {
        "prefetch": {"calendar": "lazy", "weather": "intent"},
        "intent_router": {"enabled": True, "sticky_turns": 2},
        "slag_funnel": {"enabled": True},
        "history_verbatim_tail": 4,
    },
}

_CONCIERGE_DOMAINS = [
    "calendar", "cognitive", "weather", "memory", "notify", "tasks", "search",
    "transit", "snapshots", "home",
]


class TestLazyCalendar(unittest.TestCase):
    def test_code_default_is_intent_not_lazy(self):
        self.assertEqual(calendar_prefetch_mode({}), "intent")
        self.assertEqual(calendar_prefetch_mode({"context": {}}), "intent")

    def test_lazy_skips_smithy_banter(self):
        self.assertFalse(should_prefetch_calendar(
            str(_CFG["schedule_channel_id"]), False, "concierge", "hey", _CFG,
        ))

    def test_lazy_skips_dm_schedule(self):
        self.assertFalse(should_prefetch_calendar(
            "dm", True, "concierge", "what is on today", _CFG,
        ))

    def test_intent_mode_fetches_on_schedule(self):
        cfg = {**_CFG, "context": {**_CFG["context"], "prefetch": {"calendar": "intent"}}}
        self.assertTrue(should_prefetch_calendar(
            str(cfg["schedule_channel_id"]), False, "concierge", "school today", cfg,
        ))
        self.assertFalse(should_prefetch_calendar(
            str(cfg["schedule_channel_id"]), False, "concierge", "lock the car", cfg,
        ))

    def test_always_mode_smithy(self):
        cfg = {**_CFG, "context": {**_CFG["context"], "prefetch": {"calendar": "always"}}}
        self.assertTrue(should_prefetch_calendar(
            str(cfg["schedule_channel_id"]), False, "concierge", "", cfg,
        ))

    def test_looks_schedule_intent_keywords(self):
        self.assertTrue(looks_schedule_intent("any homework tonight?"))
        self.assertFalse(looks_schedule_intent("lock the car"))


class TestIntentRouter(unittest.TestCase):
    def test_chitchat_no_tools(self):
        self.assertTrue(looks_chitchat("hey"))
        self.assertTrue(looks_chitchat("thanks!"))
        self.assertFalse(looks_chitchat("what bus is near me?"))
        domains = narrow_tool_domains(
            mode_domains=_CONCIERGE_DOMAINS,
            user_message="hey",
            config=_CFG,
            history=[],
        )
        self.assertEqual(domains, [])

    def test_bus_narrows_transit(self):
        domains = narrow_tool_domains(
            mode_domains=_CONCIERGE_DOMAINS,
            user_message="where is route 1 bus",
            config=_CFG,
            history=[],
        )
        self.assertIn("transit", domains)
        self.assertNotIn("calendar", domains)  # lazy core omits calendar
        self.assertNotIn("email", domains)

    def test_legacy_defaults_router_off(self):
        cfg = {"context": {"prefetch": {"calendar": "intent"}}}
        domains = narrow_tool_domains(
            mode_domains=_CONCIERGE_DOMAINS,
            user_message="hey",
            config=cfg,
            history=[],
        )
        self.assertEqual(domains, _CONCIERGE_DOMAINS)

    def test_ambiguous_core_only_opt_in(self):
        """2wh.14: ambiguous turns use core domains when config-gated."""
        cfg = {
            "context": {
                "intent_router": {
                    "enabled": True,
                    "sticky_turns": 0,
                    "ambiguous_core_only": True,
                },
                "prefetch": {"calendar": "lazy"},
            }
        }
        domains = narrow_tool_domains(
            mode_domains=_CONCIERGE_DOMAINS,
            user_message="tell me something interesting about jazz",
            config=cfg,
            history=[],
        )
        self.assertIsNotNone(domains)
        self.assertNotIn("email", domains or [])
        self.assertNotIn("home", domains or [])
        # core includes memory/weather/etc; calendar omitted when lazy
        self.assertNotIn("calendar", domains or [])

    def test_lazy_core_omits_calendar_on_bus(self):
        domains = narrow_tool_domains(
            mode_domains=_CONCIERGE_DOMAINS,
            user_message="where is route 1 bus",
            config=_CFG,
            history=[],
        )
        self.assertIn("transit", domains)
        self.assertNotIn("calendar", domains)

    def test_schedule_adds_calendar_on_intent_match(self):
        cfg = {
            **_CFG,
            "context": {
                **_CFG["context"],
                "prefetch": {"calendar": "intent"},
            },
        }
        domains = narrow_tool_domains(
            mode_domains=_CONCIERGE_DOMAINS,
            user_message="what's on tomorrow",
            config=cfg,
            history=[],
        )
        self.assertIn("calendar", domains)

    def test_sticky_followup_bus(self):
        history = [
            {"role": "user", "content": "track bus 1"},
            {"role": "assistant", "content": "It's on Robie."},
        ]
        domains = narrow_tool_domains(
            mode_domains=_CONCIERGE_DOMAINS,
            user_message="and the next one?",
            config=_CFG,
            history=history,
        )
        self.assertIn("transit", domains)

    def test_short_action_not_chitchat(self):
        self.assertFalse(looks_chitchat("route 1"))
        self.assertFalse(looks_chitchat("lock it"))
        self.assertFalse(looks_chitchat("today"))
        for msg in ("route 1", "lock it", "today"):
            domains = narrow_tool_domains(
                mode_domains=_CONCIERGE_DOMAINS,
                user_message=msg,
                config=_CFG,
                history=[],
            )
            self.assertNotEqual(domains, [])

    def test_sticky_lock_followup(self):
        history = [
            {"role": "user", "content": "lock the car"},
            {"role": "assistant", "content": "Locked."},
        ]
        domains = narrow_tool_domains(
            mode_domains=_CONCIERGE_DOMAINS,
            user_message="lock it",
            config=_CFG,
            history=history,
        )
        self.assertIn("home", domains)

    def test_active_surface_summary_empty(self):
        text = active_surface_summary([], 0)
        self.assertIn("none", text.lower())

    def test_router_disabled_returns_mode_domains(self):
        cfg = {
            **_CFG,
            "context": {
                **_CFG["context"],
                "intent_router": {"enabled": False},
            },
        }
        domains = narrow_tool_domains(
            mode_domains=_CONCIERGE_DOMAINS,
            user_message="hey",
            config=cfg,
            history=[],
        )
        self.assertEqual(domains, _CONCIERGE_DOMAINS)

    def test_thanks_after_bus_strips_tools(self):
        history = [
            {"role": "user", "content": "track bus 1"},
            {"role": "assistant", "content": "On Robie."},
        ]
        domains = narrow_tool_domains(
            mode_domains=_CONCIERGE_DOMAINS,
            user_message="thanks",
            config=_CFG,
            history=history,
        )
        self.assertEqual(domains, [])

    def test_anvil_skips_router(self):
        cfg = {**_CFG, "anvil_channel_id": 111}
        domains = narrow_tool_domains(
            mode_domains=_CONCIERGE_DOMAINS,
            user_message="hey",
            config=cfg,
            history=[],
            channel_id="111",
        )
        self.assertEqual(domains, _CONCIERGE_DOMAINS)

    def test_lock_niro_narrow_domains(self):
        domains = narrow_tool_domains(
            mode_domains=_CONCIERGE_DOMAINS,
            user_message="lock Niro",
            config=_CFG,
            history=[],
        )
        self.assertIn("home", domains)
        self.assertIn("snapshots", domains)
        self.assertNotIn("transit", domains)

    def test_deep_research_includes_cognitive_domain(self):
        from llm.intent_router import looks_deep_research_intent

        msg = "compare hotel options in Cape Breton for August"
        self.assertTrue(looks_deep_research_intent(msg))
        domains = narrow_tool_domains(
            mode_domains=_CONCIERGE_DOMAINS,
            user_message=msg,
            config=_CFG,
            history=[],
        )
        self.assertIn("cognitive", domains)
        self.assertIn("search", domains)

    def test_quick_fact_does_not_force_cognitive(self):
        from llm.intent_router import looks_deep_research_intent

        msg = "what time is sunset today?"
        self.assertFalse(looks_deep_research_intent(msg))
        domains = narrow_tool_domains(
            mode_domains=_CONCIERGE_DOMAINS,
            user_message=msg,
            config=_CFG,
            history=[],
        )
        self.assertNotIn("cognitive", domains or [])


class TestSurfaceNarrowed(unittest.TestCase):
    """Intent-only narrowing: post-channel ceiling equals mode ceiling."""

    @staticmethod
    def _intent_narrowed(tool_domains, mode_domains):
        return turn_surface_narrowed(tool_domains, mode_domains, mode_domains)

    def test_order_insensitive_comparison(self):
        mode = ["calendar", "weather", "transit"]
        self.assertFalse(self._intent_narrowed(sorted(mode), mode))
        self.assertTrue(self._intent_narrowed([], mode))

    def test_chitchat_empty_with_no_mode_ceiling(self):
        self.assertTrue(self._intent_narrowed([], None))
        self.assertFalse(self._intent_narrowed(None, None))

    def test_core_domains_override_respects_lazy_calendar(self):
        cfg = {
            "context": {
                "prefetch": {"calendar": "lazy"},
                "intent_router": {
                    "enabled": True,
                    "core_domains": ["calendar", "weather", "transit"],
                },
            },
        }
        self.assertNotIn("calendar", _core_domains_for(cfg))


class TestSlagFunnel(unittest.TestCase):
    def test_default_off_without_config(self):
        self.assertFalse(should_suggest_slag(
            "help me plan a sleepover",
            config={"context": {}},
            channel_id="dm",
        ))

    def test_sleepover_dm(self):
        self.assertTrue(should_suggest_slag(
            "help me plan a sleepover",
            config=_CFG,
            channel_id="dm",
        ))

    def test_not_on_slag_channel(self):
        self.assertFalse(should_suggest_slag(
            "sleepover plans",
            config=_CFG,
            channel_id=str(_CFG["slag_channel_id"]),
        ))

    def test_system_block_mentions_slag(self):
        block = slag_funnel_system_block(_CFG)
        self.assertIn("planning", block.lower())

    def test_no_false_positive_schedule_lookup(self):
        self.assertFalse(should_suggest_slag(
            "what is on the schedule tomorrow",
            config=_CFG,
            channel_id=str(_CFG["schedule_channel_id"]),
        ))
        self.assertFalse(should_suggest_slag(
            "school tomorrow",
            config=_CFG,
            channel_id=str(_CFG["schedule_channel_id"]),
        ))


class TestHistoryTail(unittest.TestCase):
    def test_default_tail_is_four(self):
        self.assertEqual(history_verbatim_tail(_CFG), 4)

    def test_prune_respects_tail(self):
        msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "big"}]}] * 6
        pruned = prune_old_tool_results(msgs, verbatim_tail=4)
        self.assertEqual(len(pruned), 6)
        self.assertEqual(pruned[0]["content"][0]["content"], "[tool result — pruned from history]")


class TestLiveSnapshot(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_sets_snapshot(self):
        set_live_snapshot(None)
        cfg = {**_CFG, "context": {**_CFG["context"], "snapshot_enabled": True}}
        with patch("presence_service.presence_service.get_presence", new_callable=AsyncMock) as gp, \
             patch("ha_service.ha_service.get_live_states", new_callable=AsyncMock) as gh:
            gp.return_value = {"dad": {"is_home": True}}
            gh.return_value = [{"entity_id": "light.kitchen", "state": "on", "attributes": {}}]
            from services.live_snapshot import refresh_live_snapshot
            snap = await refresh_live_snapshot(config=cfg, cal_service=None, session=None)
        self.assertIsNotNone(snap.presence)
        self.assertEqual(get_live_snapshot(), snap)

    def test_freshness(self):
        snap = LiveSnapshot(updated_monotonic=0)
        self.assertFalse(snap.is_fresh(60))

    async def test_ha_null_attributes(self):
        from services.live_snapshot import _ha_friendly_name
        self.assertEqual(
            _ha_friendly_name({"entity_id": "light.kitchen", "attributes": None}),
            "light.kitchen",
        )
        self.assertEqual(
            _ha_friendly_name({
                "entity_id": "light.kitchen",
                "attributes": {"friendly_name": "Kitchen"},
            }),
            "Kitchen",
        )

    async def test_ensure_fresh_timeout_returns_existing(self):
        import asyncio
        from services.live_snapshot import ensure_fresh_snapshot

        set_live_snapshot(LiveSnapshot(presence={"x": 1}, updated_monotonic=0))
        cfg = {**_CFG, "context": {**_CFG["context"], "snapshot_refresh_timeout_s": 0.05, "snapshot_enabled": True}}

        async def _slow_refresh(**kwargs):
            await asyncio.sleep(1)
            return LiveSnapshot(presence={"y": 2})

        with patch(
            "services.live_snapshot.refresh_live_snapshot",
            side_effect=_slow_refresh,
        ), patch(
            "services.live_snapshot._snapshot_refresh_timeout_s",
            return_value=0.05,
        ):
            snap = await ensure_fresh_snapshot(config=cfg, cal_service=None, session=None)
        self.assertEqual(snap.presence, {"x": 1})

    async def test_concurrent_refresh_single_flight(self):
        import asyncio
        import time
        from services.live_snapshot import ensure_fresh_snapshot, set_live_snapshot

        set_live_snapshot(None)
        cfg = {**_CFG, "context": {**_CFG["context"], "snapshot_enabled": True}}
        calls = {"n": 0}

        async def _count_refresh(**kwargs):
            calls["n"] += 1
            await asyncio.sleep(0.05)
            snap = LiveSnapshot(presence={"ok": True}, updated_monotonic=time.monotonic())
            set_live_snapshot(snap)
            return snap

        with patch(
            "services.live_snapshot.refresh_live_snapshot",
            side_effect=_count_refresh,
        ):
            await asyncio.gather(
                ensure_fresh_snapshot(config=cfg, cal_service=None, session=None),
                ensure_fresh_snapshot(config=cfg, cal_service=None, session=None),
            )
        self.assertEqual(calls["n"], 1)


class TestResolveTurnTools(unittest.TestCase):
    def test_chitchat_injects_surface_block(self):
        from unittest.mock import MagicMock, patch

        ctx = MagicMock()
        ctx.mode = MagicMock()
        ctx.allowed_domains = _CONCIERGE_DOMAINS
        ctx.person_id = "dad"
        system: list = []
        mock_gw = MagicMock()
        mock_gw.get_tool_schemas.return_value = []
        with patch("tool_gateway.get_tool_gateway", return_value=mock_gw), \
             patch("tools.get_registry", return_value={}), \
             patch("telemetry.fire_and_forget"), \
             patch("db_binding.get_database") as gdb:
            gdb.return_value.log_tool_surface = MagicMock()
            tools, domains = _resolve_turn_tools(
                config=_CFG,
                bernie_ctx=ctx,
                user_message="hey",
                history=[],
                group="family",
                cal_service=MagicMock(),
                channel_id="1",
                is_dm=False,
                live_context={},
                system=system,
            )
        self.assertEqual(domains, [])
        self.assertEqual(tools, [])
        joined = " ".join(b.get("text", "") for b in system if isinstance(b, dict))
        self.assertIn("none", joined.lower())


class TestNativeToolsPayload(unittest.TestCase):
    def test_empty_omits_tools_key(self):
        from executors.native import tools_api_payload
        self.assertEqual(tools_api_payload([]), {})
        self.assertEqual(tools_api_payload(None), {})
        self.assertIn("tools", tools_api_payload([{"name": "x"}]))


if __name__ == "__main__":
    unittest.main()
