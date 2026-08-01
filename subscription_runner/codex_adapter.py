"""Codex app-server adapter with native Bernie dynamic tools."""
from __future__ import annotations

import json
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from typing import Any, Mapping

_DEFAULT_TIMEOUT_S = 180
_MAX_LINE_BYTES = 2_000_000
_SECRETISH = re.compile(
    r"(eyJ[A-Za-z0-9_-]{10,}|Bearer\s+\S+|[\"']?(?:access|refresh)[_-]?token[\"']?\s*[:=]\s*[\"']?\S+)",
    re.I,
)
_CONTINUATIONS: dict[str, "AppServerTurn"] = {}
_CONTINUATIONS_LOCK = threading.Lock()
_MAX_CONTINUATIONS = 8


def _error(code: str, message: str, *, retryable: bool) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": _SECRETISH.sub("[redacted]", message or "")[:500],
            "retryable": retryable,
        }
    }


def _cli_env() -> dict[str, str]:
    """Minimal app-server environment; never inherit Bernie's application secrets."""
    keep = (
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
        "TERM", "TMPDIR", "TMP", "TEMP", "CODEX_HOME", "NODE_PATH", "NODE_ENV",
    )
    return {key: os.environ[key] for key in keep if os.environ.get(key)}


def _input_text(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for message in payload.get("messages") or ():
        if not isinstance(message, Mapping) or message.get("content") is None:
            continue
        content = message["content"]
        if isinstance(content, list):
            content = "\n".join(
                str(block.get("text") or "") if isinstance(block, Mapping) else str(block)
                for block in content
                if not isinstance(block, Mapping) or block.get("type") == "text"
            )
        parts.append(f"{message.get('role') or 'user'}: {content}")
    if isinstance(payload.get("prompt"), str) and payload["prompt"].strip():
        parts.append(payload["prompt"].strip())
    if not parts:
        raise ValueError("completion payload requires prompt or messages")
    return "\n\n".join(parts)


def _dynamic_tools(raw_tools: Any) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for raw in raw_tools if isinstance(raw_tools, list) else ():
        if not isinstance(raw, Mapping) or not isinstance(raw.get("name"), str):
            continue
        schema = raw.get("inputSchema") or raw.get("input_schema") or raw.get("parameters")
        tools.append({
            "type": "function",
            "name": raw["name"],
            "description": str(raw.get("description") or raw["name"])[:1024],
            "inputSchema": schema if isinstance(schema, Mapping) else {"type": "object"},
        })
    return tools


def _classify(message: str) -> dict[str, Any]:
    blob = (message or "").lower()
    if any(value in blob for value in ("login", "not authenticated", "unauthorized", "invalid_grant", "authentication")):
        return _error("auth", "Codex authentication required", retryable=True)
    if any(value in blob for value in ("quota", "rate limit", "429", "usage limit")):
        return _error("quota", "Codex quota or rate limit", retryable=True)
    if "model" in blob and any(value in blob for value in ("not found", "unsupported", "unknown")):
        return _error("invalid-request", "Codex model unavailable", retryable=False)
    return _error("upstream", message or "codex app-server failed", retryable=True)


class AppServerTurn:
    """One ephemeral app-server thread retained only across Bernie tool callbacks."""

    def __init__(self, model: str, payload: Mapping[str, Any], timeout_s: int, cancel_event: Any):
        self.model = model
        self.payload = payload
        self.deadline = time.monotonic() + timeout_s
        self.cancel_event = cancel_event
        self.request_id = 0
        self.pending: dict[str, int | str] = {}
        self.text = ""
        self.delta_text = ""
        self.usage = {"input_tokens": None, "output_tokens": None}
        self.thread_id = ""
        self.continuation_id: str | None = None
        self.stderr: deque[str] = deque(maxlen=30)
        self.stdout: queue.Queue[str | None] = queue.Queue()
        self.work = tempfile.TemporaryDirectory(prefix="codex-app-server-")
        codex_bin = shutil.which("codex")
        if not codex_bin:
            self.work.cleanup()
            raise FileNotFoundError("codex binary not on PATH")
        self.proc = subprocess.Popen(
            [codex_bin, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.work.name,
            env=_cli_env(),
            start_new_session=True,
        )
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.stdout.put(line)
        self.stdout.put(None)

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr.append(line.strip())

    def _send(self, message: Mapping[str, Any]) -> None:
        if self.proc.poll() is not None:
            raise RuntimeError("codex app-server exited")
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = dict(params)
        self._send(message)
        while True:
            reply = self._read()
            self._observe(reply)
            if reply.get("id") == request_id:
                if isinstance(reply.get("error"), Mapping):
                    raise RuntimeError(str(reply["error"].get("message") or "app-server request failed"))
                return reply.get("result") if isinstance(reply.get("result"), dict) else {}

    def _read(self) -> dict[str, Any]:
        while True:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise InterruptedError("Codex request cancelled")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex app-server turn timed out")
            if self.proc.poll() is not None:
                detail = " ".join(self.stderr) or "codex app-server exited"
                raise RuntimeError(detail)
            try:
                line = self.stdout.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if line is None:
                detail = " ".join(self.stderr) or "codex app-server exited"
                raise RuntimeError(detail)
            if len(line) > _MAX_LINE_BYTES:
                raise RuntimeError("codex app-server response exceeded limit")
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError("codex app-server emitted malformed JSONL") from exc
            if isinstance(message, dict):
                return message

    def _observe(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
        if method == "item/agentMessage/delta" and isinstance(params.get("delta"), str):
            self.delta_text += params["delta"]
        elif method == "item/completed":
            item = params.get("item") if isinstance(params.get("item"), Mapping) else {}
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                self.text = item["text"]
        elif method == "thread/tokenUsage/updated":
            token_usage = params.get("tokenUsage") if isinstance(params.get("tokenUsage"), Mapping) else {}
            last = token_usage.get("last") if isinstance(token_usage.get("last"), Mapping) else {}
            for source, target in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens")):
                value = last.get(source)
                if isinstance(value, int) and not isinstance(value, bool):
                    self.usage[target] = value

    def start(self) -> dict[str, Any]:
        self._request("initialize", {
            "clientInfo": {"name": "bernie", "title": "Bernie", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        self._send({"method": "initialized", "params": {}})
        account = self._request("account/read", {"refreshToken": False})
        if account.get("requiresOpenaiAuth") and not account.get("account"):
            raise PermissionError("Codex authentication required")
        thread_params: dict[str, Any] = {
            "model": self.model,
            "ephemeral": True,
            "cwd": self.work.name,
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "baseInstructions": str(self.payload.get("system") or ""),
            "developerInstructions": (
                "You are Bernie's language model. Use the supplied dynamic tools when "
                "the user's request needs live or private data. Never use shell, files, "
                "or web search. Return a helpful final answer after tool results."
            ),
            "dynamicTools": _dynamic_tools(self.payload.get("tools")),
        }
        thread = self._request("thread/start", thread_params)
        self.thread_id = str((thread.get("thread") or {}).get("id") or "")
        if not self.thread_id:
            raise RuntimeError("codex app-server returned no thread id")
        turn_params: dict[str, Any] = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": _input_text(self.payload)}],
            "effort": "low",
        }
        output_schema = self.payload.get("output_schema")
        if isinstance(output_schema, Mapping):
            turn_params["outputSchema"] = dict(output_schema)
        self._request("turn/start", turn_params)
        return self._next_outcome()

    def resume(self, results: Any, cancel_event: Any) -> dict[str, Any]:
        self.cancel_event = cancel_event
        if not isinstance(results, list) or not results:
            raise ValueError("tool_results required for continuation")
        for result in results:
            if not isinstance(result, Mapping):
                continue
            call_id = str(result.get("id") or "")
            rpc_id = self.pending.pop(call_id, None)
            if rpc_id is None:
                raise ValueError(f"unknown tool result id {call_id!r}")
            self._send({
                "id": rpc_id,
                "result": {
                    "contentItems": [{
                        "type": "inputText",
                        "text": str(result.get("text") or ""),
                    }],
                    "success": bool(result.get("success", True)),
                },
            })
        return self._next_outcome()

    def _next_outcome(self) -> dict[str, Any]:
        while True:
            message = self._read()
            self._observe(message)
            method = message.get("method")
            params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
            if method == "item/tool/call" and "id" in message:
                call_id = str(params.get("callId") or "")
                tool = params.get("tool")
                arguments = params.get("arguments")
                if not call_id or not isinstance(tool, str) or not isinstance(arguments, Mapping):
                    raise RuntimeError("codex app-server emitted invalid dynamic tool call")
                self.pending[call_id] = message["id"]
                if self.continuation_id is None:
                    self.continuation_id = secrets.token_urlsafe(24)
                return {
                    "text": None,
                    "tool_calls": [{"id": call_id, "name": tool, "arguments": dict(arguments)}],
                    "usage": self.usage,
                    "continuation_id": self.continuation_id,
                }
            if method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), Mapping) else {}
                if turn.get("status") != "completed" or turn.get("error"):
                    raise RuntimeError(str(turn.get("error") or f"turn {turn.get('status')}"))
                return {
                    "text": self.text or self.delta_text,
                    "tool_calls": [],
                    "usage": self.usage,
                }

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        self.work.cleanup()


def _store(turn: AppServerTurn) -> None:
    assert turn.continuation_id
    with _CONTINUATIONS_LOCK:
        if len(_CONTINUATIONS) >= _MAX_CONTINUATIONS:
            raise RuntimeError("too many pending Codex tool continuations")
        _CONTINUATIONS[turn.continuation_id] = turn
    delay = max(0.1, turn.deadline - time.monotonic())
    timer = threading.Timer(delay, _expire, args=(turn.continuation_id, turn))
    timer.daemon = True
    timer.start()


def _expire(continuation_id: str, turn: AppServerTurn) -> None:
    with _CONTINUATIONS_LOCK:
        if _CONTINUATIONS.get(continuation_id) is not turn:
            return
        _CONTINUATIONS.pop(continuation_id, None)
    turn.close()


def _take(continuation_id: str) -> AppServerTurn | None:
    with _CONTINUATIONS_LOCK:
        return _CONTINUATIONS.pop(continuation_id, None)


def codex_adapter(payload: Mapping[str, Any]) -> dict[str, Any]:
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        return {"provider": "codex", "model": "", **_error("invalid-request", "model required", retryable=False)}
    started = time.monotonic()
    continuation_id = payload.get("continuation_id")
    turn: AppServerTurn | None = None
    keep = False
    try:
        if isinstance(continuation_id, str) and continuation_id:
            turn = _take(continuation_id)
            if turn is None:
                raise ValueError("unknown or expired continuation_id")
            result = turn.resume(payload.get("tool_results"), payload.get("_cancel_event"))
        else:
            raw_timeout = payload.get("timeout_s")
            timeout_s = int(raw_timeout) if isinstance(raw_timeout, (int, float)) and raw_timeout > 0 else _DEFAULT_TIMEOUT_S
            turn = AppServerTurn(model.strip(), payload, timeout_s, payload.get("_cancel_event"))
            result = turn.start()
        if result.get("continuation_id"):
            _store(turn)
            keep = True
        return {
            "provider": "codex",
            "model": model.strip(),
            "latency_ms": (time.monotonic() - started) * 1000,
            **result,
        }
    except PermissionError as exc:
        return {"provider": "codex", "model": model.strip(), **_error("auth", str(exc), retryable=True)}
    except TimeoutError as exc:
        return {"provider": "codex", "model": model.strip(), **_error("timeout", str(exc), retryable=True)}
    except InterruptedError as exc:
        return {"provider": "codex", "model": model.strip(), **_error("unavailable", str(exc), retryable=True)}
    except ValueError as exc:
        return {"provider": "codex", "model": model.strip(), **_error("invalid-request", str(exc), retryable=False)}
    except (OSError, RuntimeError) as exc:
        return {"provider": "codex", "model": model.strip(), **_classify(str(exc))}
    finally:
        if turn is not None and not keep:
            turn.close()
