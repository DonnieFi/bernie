from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from subscription_runner.server import CapacityGate, RunnerServer, health_payload, provider_readiness


class RunnerServerTest(unittest.TestCase):
    _runner_secret = "test-runner-secret"

    def setUp(self):
        self._secret_patch = patch(
            "subscription_runner.server._RUNNER_SECRET", self._runner_secret,
        )
        self._secret_patch.start()
        self.gate = CapacityGate(max_active=1, max_queue=0, queue_timeout_s=1)
        self.server = RunnerServer(("127.0.0.1", 0), self.gate, adapters={})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._secret_patch.stop()

    def post(self, payload: dict, *, secret: str | None = _runner_secret):
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-Subscription-Runner-Auth"] = secret
        request = urllib.request.Request(
            f"{self.base}/v1/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_health_reports_readiness_without_auth_content(self):
        with tempfile.TemporaryDirectory() as codex_home, tempfile.TemporaryDirectory() as grok_home:
            secret = "never-return-this-token"
            with open(os.path.join(codex_home, "auth.json"), "w", encoding="utf-8") as auth:
                auth.write(secret)
            with (
                patch.dict(os.environ, {"CODEX_HOME": codex_home, "GROK_HOME": grok_home}),
                patch("subscription_runner.server.shutil.which", return_value="/usr/local/bin/cli"),
            ):
                payload = health_payload(self.gate)

        self.assertEqual(payload["providers"], {"codex": "ready", "grok": "reauth-required"})
        self.assertEqual(payload.get("auth_check"), "file-presence-only")
        self.assertNotIn(secret, json.dumps(payload))

    def test_grok_ready_when_auth_file_present_without_leaking_body(self):
        with tempfile.TemporaryDirectory() as codex_home, tempfile.TemporaryDirectory() as grok_home:
            secret = '{"key":"super-secret-access","refresh_token":"rt-secret","email":"hidden@example.com"}'
            with open(os.path.join(grok_home, "auth.json"), "w", encoding="utf-8") as auth:
                auth.write(secret)
            with (
                patch.dict(os.environ, {"CODEX_HOME": codex_home, "GROK_HOME": grok_home}),
                patch("subscription_runner.server.shutil.which", return_value="/usr/local/bin/grok"),
            ):
                payload = health_payload(self.gate)

        self.assertEqual(payload["providers"]["grok"], "ready")
        dumped = json.dumps(payload)
        for fragment in ("super-secret-access", "rt-secret", "hidden@example.com", "device", "user_code"):
            self.assertNotIn(fragment, dumped)
        # Only operational enums — never device codes or identity.
        self.assertIn(payload["providers"]["grok"], ("ready", "unavailable", "reauth-required"))
    def test_missing_binary_is_unavailable(self):
        with patch("subscription_runner.server.shutil.which", return_value=None):
            self.assertEqual(provider_readiness("codex", "/missing"), "unavailable")

    def test_normalized_endpoint_and_busy_response(self):
        entered = threading.Event()
        release = threading.Event()

        def adapter(payload):
            entered.set()
            release.wait(timeout=2)
            return {"provider": "codex", "model": payload["model"], "text": "ok"}

        self.server.adapters["codex"] = adapter
        first_result = []
        first = threading.Thread(
            target=lambda: first_result.append(self.post({
                "provider": "codex", "model": "gpt", "surface": "chat"
            }))
        )
        first.start()
        self.assertTrue(entered.wait(timeout=1))

        status, body = self.post({"provider": "codex", "model": "gpt", "surface": "chat"})
        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": {"code": "busy", "retryable": True}})

        release.set()
        first.join(timeout=2)
        self.assertEqual(first_result[0][0], 200)
        self.assertEqual(first_result[0][1]["text"], "ok")

    def test_unregistered_provider_is_retryable_unavailable(self):
        status, body = self.post({
            "provider": "grok", "model": "grok-build", "surface": "research"
        })
        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": {"code": "unavailable", "retryable": True}})

    def test_post_rejected_without_runner_secret(self):
        with patch("subscription_runner.server._RUNNER_SECRET", "runner-secret"):
            status, body = self.post({
                "provider": "codex", "model": "gpt", "surface": "chat",
            })
        self.assertEqual(status, 403)
        self.assertEqual(body, {"error": {"code": "forbidden", "retryable": False}})

    def test_post_allowed_with_runner_secret(self):
        self.server.adapters["codex"] = lambda payload: {
            "provider": "codex", "model": payload["model"], "text": "ok",
        }
        with patch("subscription_runner.server._RUNNER_SECRET", "runner-secret"):
            status, body = self.post(
                {"provider": "codex", "model": "gpt", "surface": "chat"},
                secret="runner-secret",
            )
        self.assertEqual(status, 200)
        self.assertEqual(body.get("text"), "ok")

    def test_empty_model_or_surface_is_invalid(self):
        status, body = self.post({"provider": "codex", "model": "", "surface": "chat"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], {"code": "invalid-request", "retryable": False})


if __name__ == "__main__":
    unittest.main()
