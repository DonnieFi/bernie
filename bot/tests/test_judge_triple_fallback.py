"""Triple-fallback chain for judges + nightly_digest fallback.

Covers the 2026-05-22 incident: Anthropic 529 cascaded into
- judge_pair / judge_triplet failures (no fallback at all)
- _call_fallback_model HTTP 401 (missing LiteLLM auth header AND
  no Ollama tail when LiteLLM itself is down)

Rule of 3: Anthropic → LiteLLM → Ollama. Walk tiers on transient
upstream failure; preserve "return None" contract only after all
three exhaust.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class _FakeModelHTTPError(Exception):
    """Stand-in for pydantic_ai.exceptions.ModelHTTPError.

    The judge fallback logic should treat any exception whose class name
    is ModelHTTPError (or which carries a 5xx/429 status_code attribute)
    as transient and move to the next tier.
    """
    def __init__(self, status_code: int, model_name: str = "claude-sonnet-4-6"):
        self.status_code = status_code
        self.model_name = model_name
        super().__init__(f"status_code: {status_code}")


def _make_result(intent=0.8, factual=0.7):
    """Build a fake PydanticAI AgentRunResult-shaped object for judge_pair."""
    res = MagicMock()
    res.output = MagicMock(
        a_intent=intent, a_factual=factual,
        b_intent=intent - 0.1, b_factual=factual - 0.1,
    )
    res.output.model_dump_json = MagicMock(return_value="{}")
    res.usage = MagicMock(input_tokens=10, output_tokens=5)
    return res


class TestJudgePairFallbackChain(unittest.TestCase):
    """judge_pair must walk Anthropic → LiteLLM → Ollama on 5xx."""

    def test_falls_through_to_litellm_on_anthropic_529(self):
        import eval_service
        # Tier 1 raises 529, tier 2 returns scores.
        agents = [MagicMock(run=AsyncMock(side_effect=_FakeModelHTTPError(529))),
                  MagicMock(run=AsyncMock(return_value=_make_result()))]
        call_log = []

        def fake_make(model, result_type):
            call_log.append(model)
            return agents.pop(0)

        import config as config_mod
        fake_cfg = {"eval": {
            "judge_fallback_model": "or-deepseek-v4",
            "judge_ollama_fallback": "hermes3:8b-llama3.1-q6_K",
        }}
        with patch.object(eval_service, "_make_judge_agent", side_effect=fake_make), \
             patch.object(eval_service, "ANTHROPIC_KEY", "sk-fake"), \
             patch.object(config_mod, "config", fake_cfg):
            result = asyncio.run(eval_service.judge_pair("p", "s", "claude-sonnet-4-6", "u"))

        self.assertIsNotNone(result, "judge_pair should return scores from tier-2 fallback, not None")
        self.assertEqual(call_log, ["claude-sonnet-4-6", "or-deepseek-v4"])

    def test_falls_through_to_ollama_when_litellm_also_fails(self):
        import eval_service
        agents = [
            MagicMock(run=AsyncMock(side_effect=_FakeModelHTTPError(529))),
            MagicMock(run=AsyncMock(side_effect=_FakeModelHTTPError(401))),  # LiteLLM auth
            MagicMock(run=AsyncMock(return_value=_make_result())),  # Ollama
        ]
        call_log = []

        def fake_make(model, result_type):
            call_log.append(model)
            return agents.pop(0)

        import config as config_mod
        fake_cfg = {"eval": {
            "judge_fallback_model": "or-deepseek-v4",
            "judge_ollama_fallback": "hermes3:8b-llama3.1-q6_K",
        }}
        with patch.object(eval_service, "_make_judge_agent", side_effect=fake_make), \
             patch.object(eval_service, "ANTHROPIC_KEY", "sk-fake"), \
             patch.object(config_mod, "config", fake_cfg):
            result = asyncio.run(eval_service.judge_pair("p", "s", "claude-sonnet-4-6", "u"))

        self.assertIsNotNone(result, "judge_pair should reach tier-3 Ollama on cascaded failure")
        self.assertEqual(call_log, [
            "claude-sonnet-4-6", "or-deepseek-v4", "hermes3:8b-llama3.1-q6_K",
        ])

    def test_returns_none_when_all_three_tiers_exhausted(self):
        import eval_service
        agents = [MagicMock(run=AsyncMock(side_effect=_FakeModelHTTPError(529)))
                  for _ in range(3)]

        def fake_make(model, result_type):
            return agents.pop(0)

        import config as config_mod
        fake_cfg = {"eval": {
            "judge_fallback_model": "or-deepseek-v4",
            "judge_ollama_fallback": "hermes3:8b-llama3.1-q6_K",
        }}
        with patch.object(eval_service, "_make_judge_agent", side_effect=fake_make), \
             patch.object(eval_service, "ANTHROPIC_KEY", "sk-fake"), \
             patch.object(config_mod, "config", fake_cfg):
            result = asyncio.run(eval_service.judge_pair("p", "s", "claude-sonnet-4-6", "u"))

        self.assertIsNone(result, "contract: return None only after all tiers fail")


class TestNightlyDigestFallback(unittest.TestCase):
    """_call_fallback_model: LiteLLM call must (a) send auth header,
    (b) cascade to Ollama on non-200."""

    def _run_with_aiohttp_status(self, litellm_status: int, ollama_text: str = "ok"):
        """Drive _call_fallback_model with a synthetic LiteLLM response.

        Ollama tail is now mocked via ``worker._call_ollama_topic`` because
        the digest fallback routes through that helper (bypasses the
        live-context injection in claude_service._call_ollama)."""
        import nightly_digest
        captured_headers = {}

        class _FakeResp:
            def __init__(self, status, body=None):
                self.status = status
                self._body = body or {"choices": [{"message": {"content": "litellm-ok"}}]}
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def json(self): return self._body

        class _FakeSess:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            def post(self, url, json=None, headers=None, ssl=None, timeout=None):
                captured_headers.update(headers or {})
                return _FakeResp(litellm_status)

        async def fake_ollama_topic(model, topic, config, num_ctx=None, system=None, timeout_s=300):
            return ollama_text, {"model": model, "tokens_in": 0, "tokens_out": 0,
                                 "duration_ms": 0, "gpu_ms": 0}

        with patch("http_session.get_http_session", return_value=_FakeSess()), \
             patch("worker._call_ollama_topic", side_effect=fake_ollama_topic):
            with patch.dict(os.environ, {"LTE_LLM_MASTER_KEY": "litellm-key-123"}):
                text = asyncio.run(nightly_digest._call_fallback_model(
                    "sys", [{"role": "user", "content": "hi"}],
                    {"litellm_base_url": "https://litellm.example.local",
                     "ollama_models": ["hermes3:8b-llama3.1-q6_K"],
                     "llm_fallback": {"model": "hermes3:8b-llama3.1-q6_K"}},
                    "or-grok",
                ))
        return text, captured_headers

    def test_litellm_path_sends_auth_header(self):
        text, headers = self._run_with_aiohttp_status(200)
        self.assertIn("Authorization", headers, "must send Authorization header to LiteLLM")
        self.assertEqual(headers["Authorization"], "Bearer litellm-key-123")
        self.assertEqual(text, "litellm-ok")

    def test_falls_through_to_ollama_on_401(self):
        text, _ = self._run_with_aiohttp_status(401, ollama_text="ollama-rescue")
        self.assertEqual(text, "ollama-rescue",
                         "401 from LiteLLM must cascade to Ollama, not return empty")

    def test_falls_through_to_ollama_on_5xx(self):
        text, _ = self._run_with_aiohttp_status(502, ollama_text="ollama-rescue")
        self.assertEqual(text, "ollama-rescue")


if __name__ == "__main__":
    unittest.main()
