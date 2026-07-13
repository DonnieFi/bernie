"""Tests for ollama_resolver — candidate selection, probing, caching, fallback.

Covers the Deba IP-flip resilience: probe candidates, cache the live host, fall
back to last-known-good (then primary) when nothing is reachable.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import ollama_resolver
from ollama_resolver import (
    _candidates,
    current_ollama_base_url,
    preflight_ollama,
    resolve_ollama_base_url,
)

A = "http://192.168.1.X:11434"
B = "http://192.168.1.Y:11434"
CFG_LIST = {"ollama_base_urls": [A, B]}
CFG_SINGLE = {"ollama_base_url": A}


class TestCandidates(unittest.TestCase):
    def setUp(self):
        ollama_resolver._reset_for_tests()

    def test_candidates_from_list(self):
        self.assertEqual(_candidates(CFG_LIST), [A, B])

    def test_candidates_from_single_fallback(self):
        self.assertEqual(_candidates(CFG_SINGLE), [A])

    def test_candidates_strip_trailing_slash(self):
        self.assertEqual(_candidates({"ollama_base_urls": [A + "/"]}), [A])

    def test_empty_list_falls_back_to_single(self):
        self.assertEqual(_candidates({"ollama_base_urls": [], "ollama_base_url": B}), [B])

    def test_current_returns_primary_when_uncached(self):
        self.assertEqual(current_ollama_base_url(CFG_LIST), A)


class TestResolve(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ollama_resolver._reset_for_tests()

    async def test_picks_first_reachable(self):
        with patch.object(ollama_resolver, "_probe", AsyncMock(return_value=True)) as p:
            url = await resolve_ollama_base_url(CFG_LIST)
        self.assertEqual(url, A)
        p.assert_awaited_once()  # stopped at the first reachable candidate

    async def test_skips_unreachable_first(self):
        # First candidate (A) unreachable, second (B) reachable.
        async def fake(url, session):
            return url == B
        with patch.object(ollama_resolver, "_probe", side_effect=fake):
            url = await resolve_ollama_base_url(CFG_LIST)
        self.assertEqual(url, B)

    async def test_none_reachable_returns_primary(self):
        with patch.object(ollama_resolver, "_probe", AsyncMock(return_value=False)):
            url = await resolve_ollama_base_url(CFG_LIST)
        self.assertEqual(url, A)  # falls back to primary candidate, doesn't crash

    async def test_none_reachable_keeps_last_known_good(self):
        # First resolve succeeds to B; later all probes fail → keep B, not primary A.
        async def only_b(url, session):
            return url == B
        with patch.object(ollama_resolver, "_probe", side_effect=only_b):
            first = await resolve_ollama_base_url(CFG_LIST, force=True)
        self.assertEqual(first, B)
        with patch.object(ollama_resolver, "_probe", AsyncMock(return_value=False)):
            second = await resolve_ollama_base_url(CFG_LIST, force=True)
        self.assertEqual(second, B)  # last-known-good preserved

    async def test_ttl_cache_avoids_reprobe(self):
        with patch.object(ollama_resolver, "_probe", AsyncMock(return_value=True)) as p:
            await resolve_ollama_base_url(CFG_LIST)        # probes once
            await resolve_ollama_base_url(CFG_LIST)        # within TTL → no probe
        self.assertEqual(p.await_count, 1)

    async def test_force_bypasses_cache(self):
        with patch.object(ollama_resolver, "_probe", AsyncMock(return_value=True)) as p:
            await resolve_ollama_base_url(CFG_LIST)
            await resolve_ollama_base_url(CFG_LIST, force=True)
        self.assertEqual(p.await_count, 2)

    async def test_current_reflects_resolved_after_probe(self):
        async def only_b(url, session):
            return url == B
        with patch.object(ollama_resolver, "_probe", side_effect=only_b):
            await resolve_ollama_base_url(CFG_LIST, force=True)
        self.assertEqual(current_ollama_base_url(CFG_LIST), B)

    async def test_fallback_prefers_current_candidates_after_reload(self):
        # Resolve to B under the old config...
        async def only_b(url, session):
            return url == B
        with patch.object(ollama_resolver, "_probe", side_effect=only_b):
            await resolve_ollama_base_url({"ollama_base_urls": [A, B]}, force=True)
        # ...then reload to a config that no longer lists B, nothing reachable.
        C = "http://192.168.1.Z:11434"
        with patch.object(ollama_resolver, "_probe", AsyncMock(return_value=False)):
            url = await resolve_ollama_base_url({"ollama_base_urls": [C]}, force=True)
        self.assertEqual(url, C)  # not the stale B

    async def test_cache_ignored_when_resolved_host_dropped_from_candidates(self):
        # Cache B (fresh), then ask with a candidate set that excludes B → the
        # fresh-cache fast path must NOT return B; it re-probes the new set.
        async def only_b(url, session):
            return url == B
        with patch.object(ollama_resolver, "_probe", side_effect=only_b):
            await resolve_ollama_base_url({"ollama_base_urls": [A, B]}, force=True)
        C = "http://192.168.1.Z:11434"
        probe = AsyncMock(return_value=True)
        with patch.object(ollama_resolver, "_probe", probe):
            url = await resolve_ollama_base_url({"ollama_base_urls": [C]})  # not forced
        self.assertEqual(url, C)
        probe.assert_awaited()  # cache was bypassed despite being within TTL

    async def test_single_flight_one_probe_for_concurrent_callers(self):
        # 5 concurrent cold-cache callers → only one probe sequence runs.
        probe = AsyncMock(return_value=True)
        with patch.object(ollama_resolver, "_probe", probe):
            await asyncio.gather(*(resolve_ollama_base_url(CFG_LIST) for _ in range(5)))
        self.assertEqual(probe.await_count, 1)


class TestPreflight(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ollama_resolver._reset_for_tests()

    async def test_preflight_reports_unreachable(self):
        with patch.object(ollama_resolver, "_probe", AsyncMock(return_value=False)):
            result = await preflight_ollama(CFG_LIST, sync_models=False)
        self.assertFalse(result["reachable"])
        self.assertEqual(result["candidates"], [A, B])

    async def test_preflight_syncs_models_when_reachable(self):
        tags_payload = {"models": [{"name": "hermes3:8b"}, {"name": "qwen3-vl:8b"}]}

        class _Resp:
            status = 200

            async def json(self):
                return tags_payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _Session:
            def get(self, *_a, **_kw):
                return _Resp()

            async def close(self):
                return None

        with patch.object(ollama_resolver, "_probe", AsyncMock(return_value=True)), \
             patch.object(ollama_resolver, "resolve_ollama_base_url", AsyncMock(return_value=B)), \
             patch("config.update_config", new_callable=AsyncMock) as mock_update:
            result = await preflight_ollama(CFG_LIST, session=_Session(), sync_models=True)

        self.assertTrue(result["reachable"])
        self.assertEqual(result["url"], B)
        self.assertEqual(result["models"], ["hermes3:8b", "qwen3-vl:8b"])
        mock_update.assert_awaited_once_with({"ollama_models": ["hermes3:8b", "qwen3-vl:8b"]})


if __name__ == "__main__":
    unittest.main()
