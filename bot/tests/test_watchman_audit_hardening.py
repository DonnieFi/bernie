"""Tests for the 2026-05-25 Watchman nightly-audit hardening.

Root cause of the misleading "Unable to Complete Nightly Infrastructure Audit"
email (run at 2026-05-25 03:00):
  1. The local Ollama draft call failed (dialed a dead NIC, see #3), so the draft
     fell back to the literal placeholder "Local summary unavailable.".
  2. generate_nightly_report fed ONLY that placeholder to the synthesis model —
     all structured data (container errors, logs, remote health, usage) was lost.
  3. Given a content-free placeholder and "report actionable failures", haiku
     confabulated a connectivity-outage narrative and told the family to check
     their home network / power-cycle the monitoring system.

These tests lock in the four fixes:
  #1 structured facts always reach synthesis; no content-free placeholder.
  #2 the audit system prompt forbids inventing network/hardware advice.
  #3 the resolved (probed-live) Ollama host is never clobbered by a stale
     llm_fallback.url.
  #4 a Docker-unavailable container scan is reported as "couldn't check", never
     as "all clear".
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Mock heavy/optional modules before importing Bernie code (mirrors test_phase24).
sys.modules.setdefault("discord", MagicMock())
sys.modules.setdefault("discord.ext", MagicMock())
sys.modules.setdefault("discord.ext.tasks", MagicMock())
sys.modules.setdefault("anthropic", MagicMock())
sys.modules.setdefault("websockets", MagicMock())

sys.path.append(os.getcwd())
sys.path.append(os.path.join(os.getcwd(), "bot"))

import watchman
from watchman import Watchman, _build_facts_block
from llm.ollama import resolve_ollama_target as _resolve_ollama_target
from llm.audit import AUDIT_SYSTEM_PROMPT

# Phrases that should NEVER appear in audit output unless a remote-health entity
# is explicitly offline — they are the hallmark of the hallucinated email.
_NETWORK_ADVICE = (
    "check your home network",
    "power-cycle",
    "has power",
    "monitoring system",
    "connectivity issue on my end",
    "internet connection",
)


class TestBuildFactsBlock(unittest.TestCase):
    def test_empty_inputs_produce_factual_block_without_network_advice(self):
        facts = _build_facts_block(
            errors={}, bot_logs=[], remote={}, usage={}, docker_available=True
        )
        self.assertTrue(facts.strip(), "facts block must not be empty")
        lower = facts.lower()
        for phrase in _NETWORK_ADVICE:
            self.assertNotIn(phrase, lower, f"must not emit hardware advice: {phrase!r}")

    def test_docker_unavailable_is_distinguished_from_no_errors(self):
        facts = _build_facts_block(
            errors={}, bot_logs=[], remote={}, usage={}, docker_available=False
        ).lower()
        # Must signal that the container scan could not run...
        self.assertTrue(
            "unavailable" in facts or "could not" in facts or "not mounted" in facts,
            "Docker-unavailable must be reported as a non-result",
        )
        # ...and must NOT claim containers are clean/healthy when it never looked.
        self.assertNotIn("no container errors", facts)

    def test_container_errors_are_included(self):
        facts = _build_facts_block(
            errors={"family-bot": ["2026-05-25 [ERROR] kaboom in worker"]},
            bot_logs=[],
            remote={},
            usage={},
            docker_available=True,
        )
        self.assertIn("family-bot", facts)
        self.assertIn("kaboom", facts)

    def test_tool_calls_are_counted_and_included(self):
        tool_calls = [
            {"description": "Tool <b>get_sleep_summary</b> called via native"},
            {"description": "Tool <b>get_sleep_summary</b> called via native"},
            {"description": "Tool <b>get_vehicle_status</b> called via native"},
            {"description": "Tool <b>get_route_buses</b> called via native"},
            {"description": "Malformed tool call without bold tag"},
        ]
        facts = _build_facts_block(
            errors={},
            bot_logs=[],
            remote={},
            usage={},
            docker_available=True,
            tool_calls=tool_calls,
        )
        self.assertIn("Tool calls (24h):", facts)
        self.assertIn("- get_sleep_summary: 2", facts)
        self.assertIn("- get_vehicle_status: 1", facts)
        self.assertIn("- get_route_buses: 1", facts)
        self.assertNotIn("Malformed", facts)


class TestResolveOllamaTarget(unittest.TestCase):
    def test_resolved_live_host_wins_over_stale_fallback_url(self):
        cfg = {"llm_fallback": {"url": "http://192.168.1.X:11434", "model": "hermes3:8b"}}
        base_url, model = _resolve_ollama_target(cfg, "http://192.168.1.Y:11434", None)
        self.assertEqual(base_url, "http://192.168.1.Y:11434")
        self.assertEqual(model, "hermes3:8b")

    def test_model_override_keeps_resolved_host(self):
        cfg = {"llm_fallback": {"url": "http://192.168.1.X:11434", "model": "hermes3:8b"}}
        base_url, model = _resolve_ollama_target(cfg, "http://192.168.1.Y:11434", "qwen2.5")
        self.assertEqual(base_url, "http://192.168.1.Y:11434")
        self.assertEqual(model, "qwen2.5")

    def test_no_fallback_config_returns_none_model(self):
        base_url, model = _resolve_ollama_target({}, "http://192.168.1.Y:11434", None)
        self.assertEqual(base_url, "http://192.168.1.Y:11434")
        self.assertIsNone(model)


class TestAuditSystemPrompt(unittest.TestCase):
    def test_prompt_forbids_inventing_network_advice(self):
        lower = AUDIT_SYSTEM_PROMPT.lower()
        self.assertIn("home network", lower)
        # The guard must reference reporting only on provided data.
        self.assertTrue(
            "only" in lower and ("provided" in lower or "data" in lower),
            "prompt must constrain the model to the provided data",
        )

    def test_audit_model_is_config_only(self):
        """Phase 4.4: audit synthesis must not reintroduce hardcoded model IDs."""
        with open(os.path.join(os.getcwd(), "llm", "audit.py")) as f:
            audit_src = f.read()
        self.assertNotIn("claude-sonnet-4-6", audit_src)
        self.assertIn('cfg.get("audit_model")', audit_src)


class TestGenerateNightlyReportDataSurvival(unittest.IsolatedAsyncioTestCase):
    async def test_draft_failure_does_not_drop_structured_data(self):
        """When the Ollama draft fails, real container errors must still reach
        synthesis — never replaced by the 'Local summary unavailable.' placeholder."""
        wm = Watchman()
        wm._docker_available = True  # deterministic regardless of where the test runs
        wm.get_recent_errors = AsyncMock(return_value={"family-bot": ["[ERROR] kaboom"]})
        wm.get_bernie_logs = AsyncMock(return_value=[])
        wm.get_remote_health = AsyncMock(return_value={})
        wm.get_llm_usage_summary = AsyncMock(return_value={})

        async def _echo(draft, cfg, container=None):
            return draft  # surface exactly what synthesis received

        with patch.object(watchman, "config", {}), \
             patch("llm.ollama.call_ollama", AsyncMock(side_effect=RuntimeError("ollama down"))), \
             patch("llm.audit.call_for_audit", AsyncMock(side_effect=_echo)):
            report = await wm.generate_nightly_report()

        self.assertIn("kaboom", report, "real error data must survive a draft failure")
        self.assertNotEqual(report.strip(), "Local summary unavailable.")
        lower = report.lower()
        for phrase in _NETWORK_ADVICE:
            self.assertNotIn(phrase, lower)

    async def test_synthesis_passes_container_to_call_for_audit(self):
        """Phase 4.4: call_for_audit requires ServiceContainer for llm_for routing."""
        wm = Watchman()
        wm._docker_available = True
        wm.get_recent_errors = AsyncMock(return_value={})
        wm.get_bernie_logs = AsyncMock(return_value=[])
        wm.get_remote_health = AsyncMock(return_value={})
        wm.get_llm_usage_summary = AsyncMock(return_value={})

        sentinel = object()
        audit_mock = AsyncMock(return_value="ok")

        with patch.object(watchman, "config", {"audit_model": "claude-haiku-4-5-20251001"}), \
             patch("llm.ollama.call_ollama", AsyncMock(return_value="draft")), \
             patch("llm.audit.call_for_audit", audit_mock), \
             patch("llm.runtime.get_container", return_value=sentinel):
            await wm.generate_nightly_report()

        audit_mock.assert_awaited_once()
        self.assertIs(audit_mock.await_args.args[2], sentinel)


if __name__ == "__main__":
    unittest.main()
