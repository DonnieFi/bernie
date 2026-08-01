"""Behavior tests for Discord model-auth / provider + ToolGateway status (3an.7).

Covers ready / reauth-required, provider filtering, usage=unavailable without
fabricated zeros, admin-only ToolGateway access, anvil/admin ephemeral policy
guards (via helpers), and secret exclusion. Reuses normalized usage contract.
"""
from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestFormatModelAuthMessage(unittest.TestCase):
    def test_ready(self):
        from subscription_complete import format_model_auth_message
        msg = format_model_auth_message("grok", "ready")
        self.assertIn("ready", msg.lower())
        self.assertIn("grok", msg.lower())
        self.assertNotIn("device", msg.lower())
        self.assertNotIn("token", msg.lower())

    def test_reauth_required(self):
        from subscription_complete import format_model_auth_message
        msg = format_model_auth_message("codex", "reauth-required")
        self.assertIn("reauth-required", msg)
        self.assertIn("SSH", msg)
        self.assertIn("Do not paste tokens", msg)
        # No fabricated secrets
        for banned in ("sk-", "device_code", "auth.json", "@", "password"):
            self.assertNotIn(banned, msg)


class TestUsageSnapshot(unittest.IsolatedAsyncioTestCase):
    async def test_usage_unavailable_no_zeros(self):
        from subscription_complete import subscription_usage_snapshot

        with patch(
            "subscription_complete.fetch_runner_health",
            new=AsyncMock(return_value={
                "providers": {"codex": "ready", "grok": "reauth-required"},
                "email": "leak@x.ai",
                "token": "secret",
            }),
        ):
            text = await subscription_usage_snapshot({})
        self.assertIn("usage=unavailable", text)
        self.assertIn("quota=unavailable", text)
        self.assertIn("reset=unavailable", text)
        self.assertIn("readiness=ready", text)
        self.assertIn("readiness=reauth-required", text)
        # Never fabricate numeric zeros for usage
        self.assertNotIn("usage=0", text)
        self.assertNotIn("tokens=0", text)
        self.assertNotIn("leak@x.ai", text)
        self.assertNotIn("secret", text)

    async def test_provider_filter(self):
        from subscription_complete import subscription_usage_snapshot

        with patch(
            "subscription_complete.fetch_runner_health",
            new=AsyncMock(return_value={
                "providers": {"codex": "ready", "grok": "unavailable"},
            }),
        ):
            only_grok = await subscription_usage_snapshot({}, provider="grok")
        self.assertIn("grok", only_grok)
        self.assertNotIn("codex", only_grok)


class TestFetchRunnerHealthRedaction(unittest.IsolatedAsyncioTestCase):
    async def test_strips_identity_and_unknown_states(self):
        from subscription_complete import fetch_runner_health

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def json(self, content_type=None):
                return {
                    "status": "ok",
                    "providers": {
                        "codex": "ready",
                        "grok": "reauth-required",
                        "email": "a@b.c",
                        "weird": "ready",
                    },
                    "account": "user-123",
                    "device_code": "XXXX",
                    "auth_file": "/secret/path",
                }

        class _Sess:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, *a, **k):
                return _Resp()

        with patch("aiohttp.ClientSession", return_value=_Sess()):
            health = await fetch_runner_health({"subscription_runner_url": "http://r"})

        self.assertEqual(health["providers"], {
            "codex": "ready",
            "grok": "reauth-required",
        })
        blob = repr(health)
        for banned in ("a@b.c", "user-123", "XXXX", "/secret/path", "email", "account"):
            self.assertNotIn(banned, blob)


class TestGetProviderStatusTool(unittest.IsolatedAsyncioTestCase):
    async def test_ready_and_reauth_in_output(self):
        from tools.admin import handle_get_provider_status

        cfg = {
            "subscription_models": [{
                "provider": "grok",
                "model": "grok-4.5",
                "capabilities": ["text", "tools"],
                "openrouter_fallback_model": "x-ai/grok-4.5",
                "enabled": True,
            }],
            "ollama_models": ["qwen"],
            "llm_fallback": {"model": "qwen"},
            "anthropic_models": [],
            "litellm_models": [],
        }
        ctx = SimpleNamespace(config=cfg, shadow=False, group="admin")
        with (
            patch(
                "subscription_complete.fetch_runner_health",
                new=AsyncMock(return_value={
                    "providers": {"codex": "ready", "grok": "reauth-required"},
                }),
            ),
            patch("llm.model_state.get_model_info", return_value=("grok-4.5", None)),
        ):
            out = await handle_get_provider_status({}, ctx)
        self.assertIn("codex: ready", out)
        self.assertIn("grok: reauth-required", out)
        self.assertIn("usage=unavailable", out)
        self.assertIn("Selected model: `grok-4.5`", out)
        self.assertNotIn("device_code", out)
        self.assertNotIn("sk-", out)

    async def test_provider_filter_on_tool(self):
        from tools.admin import handle_get_provider_status

        cfg = {"ollama_models": ["qwen"], "anthropic_models": [], "litellm_models": []}
        ctx = SimpleNamespace(config=cfg, shadow=False, group="admin")
        with (
            patch(
                "subscription_complete.fetch_runner_health",
                new=AsyncMock(return_value={
                    "providers": {"codex": "ready", "grok": "unavailable"},
                }),
            ),
            patch("llm.model_state.get_model_info", return_value=("qwen", None)),
        ):
            out = await handle_get_provider_status({"provider": "grok"}, ctx)
        self.assertIn("grok", out)
        # Readiness list filtered; codex line should not appear as readiness bullet
        readiness_lines = [ln for ln in out.splitlines() if ln.startswith("• ")]
        self.assertTrue(any("grok" in ln for ln in readiness_lines))
        self.assertFalse(any("codex" in ln and "readiness" not in ln for ln in readiness_lines if "usage" not in ln))
        # Usage section also filtered
        self.assertNotIn("• codex:", out)


class TestAdminOnlyToolGateway(unittest.TestCase):
    def test_get_provider_status_role_admin(self):
        from tools import get_registry, load_all_domains
        load_all_domains()
        reg = get_registry()
        entry = reg.get("get_provider_status")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.get("role_required"), "admin")

    def test_gateway_hides_from_family_group(self):
        from tool_gateway import ToolGateway, _role_allowed
        from tools import get_registry, load_all_domains
        load_all_domains()
        self.assertFalse(_role_allowed("family", "admin"))
        self.assertFalse(_role_allowed("parents", "admin"))
        self.assertTrue(_role_allowed("admin", "admin"))
        self.assertTrue(_role_allowed("system", "admin"))

        gw = ToolGateway(get_registry())
        family_names = {s["name"] for s in gw.get_tool_schemas("family")}
        admin_names = {s["name"] for s in gw.get_tool_schemas("admin")}
        self.assertNotIn("get_provider_status", family_names)
        self.assertIn("get_provider_status", admin_names)


class TestSlashEphemeralGuards(unittest.TestCase):
    """Slash model-auth / provider require anvil + admin; responses are ephemeral."""

    def test_model_auth_helpers_called_with_ephemeral(self):
        # Inspect source contract without importing discord (host may lack audioop).
        from pathlib import Path
        src = Path(__file__).resolve().parents[1].joinpath("slash/admin_cmds.py").read_text()
        self.assertIn('name="model-auth"', src)
        self.assertIn('name="provider"', src)
        # Both paths force ephemeral
        self.assertIn("ephemeral=True", src)
        self.assertIn("_is_anvil", src)
        self.assertIn("_is_admin_group", src)
        self.assertIn("format_model_auth_message", src)

    def test_parity_maps_to_get_provider_status(self):
        from tests.test_slash_tool_parity import SLASH_TOOL_EQUIV
        self.assertEqual(SLASH_TOOL_EQUIV.get("model-auth"), "get_provider_status")
        self.assertEqual(SLASH_TOOL_EQUIV.get("provider"), "get_provider_status")


if __name__ == "__main__":
    unittest.main()
