"""Hardened internal HTTP seam for subscription-backed CLI adapters."""
from __future__ import annotations

import json
import os
import select
import shutil
import socket
import threading
import time
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

Adapter = Callable[[dict], dict]
MAX_BODY_BYTES = 1_048_576
_RUNNER_SECRET = (os.environ.get("SUBSCRIPTION_RUNNER_SECRET") or "").strip()


def _positive_int(name: str, default: int, *, allow_zero: bool = False) -> int:
    value = int(os.environ.get(name, default))
    if value < (0 if allow_zero else 1):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return value


class CapacityGate:
    """Bound active requests and queued waiters without spawning extra work."""

    def __init__(self, max_active: int, max_queue: int, queue_timeout_s: int = 30):
        if max_active < 1 or max_queue < 0 or queue_timeout_s < 1:
            raise ValueError("invalid runner capacity limits")
        self.max_active = max_active
        self.max_queue = max_queue
        self.queue_timeout_s = queue_timeout_s
        self._slots = threading.BoundedSemaphore(max_active)
        self._lock = threading.Lock()
        self._active = 0
        self._queued = 0

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "active": self._active,
                "queued": self._queued,
                "max_active": self.max_active,
                "max_queue": self.max_queue,
            }

    @contextmanager
    def admit(self, cancel_event=None):
        """Acquire a slot; poll cancel_event while waiting in the queue."""
        with self._lock:
            rejected = self._active + self._queued >= self.max_active + self.max_queue
            if not rejected:
                self._queued += 1
        if rejected:
            yield False
            return

        deadline = time.monotonic() + self.queue_timeout_s
        acquired = False
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                break
            acquired = self._slots.acquire(timeout=0.25)
            if acquired:
                break
        with self._lock:
            self._queued -= 1
            if acquired:
                self._active += 1
        try:
            yield acquired
        finally:
            if acquired:
                with self._lock:
                    self._active -= 1
                self._slots.release()


def provider_readiness(executable: str, home: str) -> str:
    """Return operational readiness; never inspect or expose auth content.

    ``ready`` means ``auth.json`` exists only — expired sessions surface as
    retryable ``auth`` errors on the first completion, not at probe time.
    """
    if shutil.which(executable) is None:
        return "unavailable"
    return "ready" if Path(home, "auth.json").is_file() else "reauth-required"


def health_payload(gate: CapacityGate) -> dict:
    return {
        "status": "ok",
        "providers": {
            "codex": provider_readiness("codex", os.environ["CODEX_HOME"]),
            "grok": provider_readiness("grok", os.environ["GROK_HOME"]),
        },
        "capacity": gate.snapshot(),
        "auth_check": "file-presence-only",
    }


def _post_authorized(handler: BaseHTTPRequestHandler) -> bool:
    """POST /v1/completions requires shared secret (fail closed when unset)."""
    if not _RUNNER_SECRET:
        return False
    return handler.headers.get("X-Subscription-Runner-Auth") == _RUNNER_SECRET


class RunnerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, gate: CapacityGate, adapters: dict[str, Adapter]):
        self.gate = gate
        self.adapters = adapters
        super().__init__(address, RunnerHandler)


class RunnerHandler(BaseHTTPRequestHandler):
    server: RunnerServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        # Request paths/statuses only; completion payloads and auth never enter logs.
        super().log_message(fmt, *args)

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, code: str, *, retryable: bool) -> None:
        self._send_json(status, {"error": {"code": code, "retryable": retryable}})

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler interface
        if self.path != "/health":
            self._error(HTTPStatus.NOT_FOUND, "not-found", retryable=False)
            return
        self._send_json(HTTPStatus.OK, health_payload(self.server.gate))

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler interface
        if self.path != "/v1/completions":
            self._error(HTTPStatus.NOT_FOUND, "not-found", retryable=False)
            return
        if not _post_authorized(self):
            self._error(HTTPStatus.FORBIDDEN, "forbidden", retryable=False)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 2 or length > MAX_BODY_BYTES:
            self._error(HTTPStatus.BAD_REQUEST, "invalid-request", retryable=False)
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid-request", retryable=False)
            return
        if not isinstance(payload, dict):
            self._error(HTTPStatus.BAD_REQUEST, "invalid-request", retryable=False)
            return
        provider = payload.get("provider")
        model = payload.get("model")
        surface = payload.get("surface")
        if (
            provider not in ("codex", "grok")
            or not isinstance(model, str)
            or not model.strip()
            or not isinstance(surface, str)
            or not surface.strip()
        ):
            self._error(HTTPStatus.BAD_REQUEST, "invalid-request", retryable=False)
            return
        adapter = self.server.adapters.get(provider)
        if adapter is None:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "unavailable", retryable=True)
            return

        cancelled = threading.Event()
        finished = threading.Event()

        def watch_disconnect() -> None:
            while not finished.wait(0.25):
                readable, _, _ = select.select([self.connection], [], [], 0)
                if not readable:
                    continue
                try:
                    if self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                        continue
                except BlockingIOError:
                    continue
                except OSError:
                    pass
                cancelled.set()
                return

        # Watch disconnect before queue wait so abandoned clients free capacity.
        threading.Thread(target=watch_disconnect, daemon=True).start()
        try:
            with self.server.gate.admit(cancel_event=cancelled) as admitted:
                if not admitted:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "busy", retryable=True)
                    return
                if cancelled.is_set():
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "unavailable", retryable=True)
                    return
                try:
                    internal_payload = dict(payload)
                    internal_payload["_cancel_event"] = cancelled
                    result = adapter(internal_payload)
                    if not isinstance(result, dict):
                        raise TypeError("adapter result must be an object")
                except Exception:
                    self._error(HTTPStatus.BAD_GATEWAY, "upstream", retryable=True)
                    return
        finally:
            finished.set()
        # Adapters may embed typed errors (auth/quota/timeout/…) without raising.
        # Still HTTP 200 so callers read the normalized body; capacity busy stays 503.
        self._send_json(HTTPStatus.OK, result)


def _load_adapters() -> dict[str, Adapter]:
    """Register CLI adapters available in this image. Missing modules are skipped."""
    adapters: dict[str, Adapter] = {}
    try:
        from grok_adapter import grok_adapter
        adapters["grok"] = grok_adapter
    except ImportError:
        pass
    try:
        from codex_adapter import codex_adapter
        adapters["codex"] = codex_adapter
    except ImportError:
        pass
    return adapters


def main() -> None:
    gate = CapacityGate(
        _positive_int("RUNNER_MAX_ACTIVE", 2),
        _positive_int("RUNNER_MAX_QUEUE", 4, allow_zero=True),
        _positive_int("RUNNER_QUEUE_TIMEOUT_S", 30),
    )
    port = _positive_int("RUNNER_PORT", 8080)
    RunnerServer(("0.0.0.0", port), gate, adapters=_load_adapters()).serve_forever()


if __name__ == "__main__":
    main()
