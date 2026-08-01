"""Headless Grok CLI adapter (text path only).

Invokes the official ``grok`` binary with argv (never a shell), empty cwd,
built-ins disabled, and normalizes final text / usage / typed errors.
Tool envelopes and output-schema repair are out of scope (family-bot-tho.4).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping

# Match CompletionErrorCode string values in bot/completion_router.py without
# importing Bernie application code into the isolated runner image.
_ERROR_CODES = frozenset({
    "timeout", "auth", "quota", "unavailable", "busy",
    "upstream", "schema", "invalid-request", "malformed",
})

_DEFAULT_TIMEOUT_S = 120
_MAX_OUTPUT_BYTES = 2_000_000
_SECRETISH = re.compile(
    r"(eyJ[A-Za-z0-9_-]{10,}"
    r"|Bearer\s+\S+"
    r"|(?:access|refresh)[_-]?token\s*[:=]\s*\S+"
    r"|[\"'](?:access|refresh)[_-]?token[\"']\s*:\s*[\"'][^\"']+[\"']"
    r")",
    re.I,
)


def _cli_env() -> dict[str, str]:
    """Minimal env for CLI — never inherit full Bernie .env secrets."""
    keep = (
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
        "TERM", "TMPDIR", "TMP", "TEMP", "GROK_HOME", "NODE_PATH", "NODE_ENV",
    )
    env = {k: os.environ[k] for k in keep if k in os.environ and os.environ[k]}
    env["GROK_TELEMETRY_ENABLED"] = "0"
    if "GROK_HOME" in os.environ:
        env["GROK_HOME"] = os.environ["GROK_HOME"]
    return env


def _timeout_s() -> int:
    try:
        value = int(os.environ.get("GROK_ADAPTER_TIMEOUT_S", _DEFAULT_TIMEOUT_S))
    except ValueError:
        value = _DEFAULT_TIMEOUT_S
    return max(5, min(value, 600))


def _sanitize(message: str) -> str:
    cleaned = _SECRETISH.sub("[redacted]", message or "")
    return cleaned[:500]


def _error(code: str, message: str = "", *, retryable: bool) -> dict[str, Any]:
    if code not in _ERROR_CODES:
        code = "upstream"
    return {
        "error": {
            "code": code,
            "message": _sanitize(message),
            "retryable": retryable,
        }
    }


def _prompt_from_payload(payload: Mapping[str, Any]) -> str:
    """Build a single-turn prompt from the normalized completion payload.

    Must include system + message history even when ``prompt`` is set — Bernie
    always sends prompt alongside messages for the current user turn.
    """
    parts: list[str] = []
    system = payload.get("system")
    if isinstance(system, str) and system.strip():
        parts.append(system.strip())

    messages = payload.get("messages")
    if isinstance(messages, (list, tuple)):
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or "user")
            content = message.get("content")
            if isinstance(content, list):
                # Anthropic-style content blocks → text only.
                chunks = []
                for block in content:
                    if isinstance(block, Mapping) and block.get("type") == "text":
                        chunks.append(str(block.get("text") or ""))
                    elif isinstance(block, str):
                        chunks.append(block)
                content = "\n".join(chunks)
            if content is None:
                continue
            text = str(content).strip()
            if not text:
                continue
            parts.append(f"{role}: {text}")

    prompt = payload.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        prompt = prompt.strip()
        if not parts or parts[-1] != f"user: {prompt}":
            parts.append(f"user: {prompt}")

    if not parts:
        raise ValueError("completion payload requires prompt or messages")
    return "\n\n".join(parts)


def _classify_cli_failure(returncode: int, stderr: str, stdout: str) -> dict[str, Any]:
    blob = f"{stderr}\n{stdout}".lower()
    if returncode in (-signal.SIGKILL, -9) or returncode == 124:
        return _error("timeout", "grok CLI timed out or was killed", retryable=True)
    if any(token in blob for token in (
        "re-auth", "reauth", "not authenticated", "login required",
        "sign in", "auth token", "unauthorized", "invalid_grant",
    )):
        return _error("auth", "grok CLI authentication required", retryable=True)
    if any(token in blob for token in ("quota", "rate limit", "429", "resource_exhausted")):
        return _error("quota", "grok CLI quota or rate limit", retryable=True)
    if any(token in blob for token in ("busy", "too many", "resource temporarily")):
        return _error("busy", "grok CLI busy", retryable=True)
    if "unknown model" in blob or "invalid params" in blob:
        return _error("invalid-request", _sanitize(stderr or stdout), retryable=False)
    if "not found" in blob and "grok" in blob:
        return _error("unavailable", "grok binary missing", retryable=True)
    return _error("upstream", _sanitize(stderr or stdout or f"exit {returncode}"), retryable=True)


def _parse_usage(raw: Mapping[str, Any]) -> dict[str, int | None]:
    """Extract token usage only when numeric fields are present — never invent."""
    usage_raw = raw.get("usage")
    if not isinstance(usage_raw, Mapping):
        return {"input_tokens": None, "output_tokens": None}

    def _num(key: str) -> int | None:
        value = usage_raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    return {
        "input_tokens": _num("input_tokens"),
        "output_tokens": _num("output_tokens"),
    }


def _parse_first_json_object(text: str) -> dict[str, Any] | None:
    """Decode the first JSON object from a string (handles concatenated blobs)."""
    stripped = (text or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    try:
        obj, _ = json.JSONDecoder().raw_decode(stripped)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_envelope_from_text(text: str) -> dict[str, Any] | None:
    """Parse tool/final envelope from plain, fenced, or prose-prefixed JSON in CLI text."""
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    direct = _parse_first_json_object(stripped)
    if isinstance(direct, dict) and direct.get("type") in ("final", "tool_calls"):
        return direct

    # Grok often prefixes prose before the JSON envelope in the CLI ``text`` field.
    for marker in (
        '{"type":"tool_calls"',
        '{"type": "tool_calls"',
        '{"type":"final"',
        '{"type": "final"',
    ):
        idx = stripped.find(marker)
        if idx < 0:
            continue
        parsed = _parse_first_json_object(stripped[idx:])
        if isinstance(parsed, dict) and parsed.get("type") in ("final", "tool_calls"):
            return parsed
    return None


def _parse_json_output(stdout: str, *, expect_envelope: bool = False) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return _error("malformed", "empty grok CLI stdout", retryable=False)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Plain text fallback if CLI ignored --output-format json.
        return {"text": text, "usage": {"input_tokens": None, "output_tokens": None}}
    if not isinstance(data, dict):
        return _error("malformed", "grok CLI JSON was not an object", retryable=False)

    usage = _parse_usage(data)
    stop = data.get("stopReason")

    if expect_envelope:
        so_err = data.get("structuredOutputError")
        if isinstance(so_err, str) and so_err.strip():
            structured_probe = data.get("structuredOutput")
            if not (
                isinstance(structured_probe, dict)
                and structured_probe.get("type") in ("final", "tool_calls")
            ):
                return _error("schema", so_err.strip(), retryable=True)

    # Grok CLI puts --json-schema output in structuredOutput; text may hold a JSON string.
    structured = data.get("structuredOutput")
    if isinstance(structured, dict) and structured.get("type") in ("final", "tool_calls"):
        envelope: dict[str, Any] = structured
    elif data.get("type") in ("final", "tool_calls"):
        envelope = data
    elif expect_envelope:
        inner = data.get("text")
        parsed_inner = _extract_envelope_from_text(inner) if isinstance(inner, str) else None
        envelope = parsed_inner if parsed_inner else data
    else:
        envelope = data

    # Structured tool / final envelope (tho.4)
    if expect_envelope or envelope.get("type") in ("final", "tool_calls"):
        kind = envelope.get("type")
        if kind == "tool_calls":
            calls = envelope.get("tool_calls") or []
            if not isinstance(calls, list) or not calls:
                return _error("schema", "tool_calls envelope empty", retryable=True)
            normalized = []
            for i, call in enumerate(calls):
                if not isinstance(call, Mapping):
                    return _error("schema", "tool_call not an object", retryable=True)
                name = call.get("name")
                args = call.get("arguments")
                if not isinstance(name, str) or not name.strip():
                    return _error("schema", "tool_call missing name", retryable=True)
                if not isinstance(args, Mapping):
                    return _error("schema", "tool_call arguments must be object", retryable=True)
                normalized.append({
                    "id": str(call.get("id") or f"call_{i}"),
                    "name": name.strip(),
                    "arguments": dict(args),
                })
            return {
                "text": None,
                "tool_calls": normalized,
                "usage": usage,
                "stop_reason": stop,
            }
        if kind == "final":
            final = envelope.get("text")
            return {
                "text": "" if final is None else str(final),
                "tool_calls": [],
                "usage": usage,
                "stop_reason": stop,
            }
        # Expecting envelope but model returned plain text under CLI wrapper.
        if isinstance(data.get("text"), str):
            return {
                "text": str(data["text"]),
                "tool_calls": [],
                "usage": usage,
                "stop_reason": stop,
            }
        if expect_envelope and stop == "Cancelled":
            return _error(
                "schema",
                "grok cancelled without structured tool/final envelope",
                retryable=True,
            )
        return _error("schema", "envelope missing final text or tool_calls", retryable=True)

    final = data.get("text")
    if final is None:
        return _error("malformed", "grok CLI JSON missing text", retryable=False)
    return {
        "text": str(final),
        "tool_calls": [],
        "usage": usage,
        "stop_reason": stop,
    }
_TOOL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["final", "tool_calls"]},
        "text": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["name", "arguments"],
            },
        },
    },
    "required": ["type"],
}


def build_grok_argv(
    model: str,
    cwd: str,
    *,
    prompt_file: str,
    json_schema: dict[str, Any] | None = None,
    max_turns: int = 1,
) -> list[str]:
    """Argv for a tool-disabled single-turn headless completion.

    Prompt is read from ``prompt_file`` (``--prompt-file``). Grok's ``-p -`` is
    the literal prompt ``"-"``, not stdin — never use it for Bernie payloads.
    """
    turns = max(1, min(int(max_turns), 8))
    argv = [
        "grok",
        "--prompt-file", prompt_file,
        "-m", model,
        "--output-format", "json",
        "--disable-web-search",
        "--no-subagents",
        "--no-plan",
        "--no-memory",
        "--permission-mode", "dontAsk",
        "--max-turns", str(turns),
        "--cwd", cwd,
        "--disallowed-tools", "Bash,Edit,Write,Read,Glob,Grep,WebSearch,WebFetch,Task",
    ]
    if json_schema is not None:
        argv.extend(["--json-schema", json.dumps(json_schema, separators=(",", ":"))])
    return argv

def run_grok_cli(
    model: str,
    prompt: str,
    *,
    timeout_s: int | None = None,
    executable: str | None = None,
    json_schema: dict[str, Any] | None = None,
    expect_envelope: bool = False,
    cancel_event: Any = None,
    max_turns: int | None = None,
) -> dict[str, Any]:
    """Run headless grok and return normalized success or error payload fields."""
    grok_bin = executable or shutil.which("grok")
    if not grok_bin:
        return _error("unavailable", "grok binary not on PATH", retryable=True)

    timeout = timeout_s if timeout_s is not None else _timeout_s()
    work = tempfile.mkdtemp(prefix="grok-adapter-")
    try:
        # Refuse to run if workdir is not empty after create (paranoia).
        if any(Path(work).iterdir()):
            return _error("upstream", "work directory not empty", retryable=False)

        prompt_path = Path(work) / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        turns = max_turns if max_turns is not None else (3 if expect_envelope else 1)
        argv = build_grok_argv(
            model, work, prompt_file=str(prompt_path), json_schema=json_schema,
            max_turns=turns,
        )
        argv[0] = grok_bin
        env = _cli_env()

        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=work,
                env=env,
                start_new_session=True,  # own process group for killpg
            )
        except OSError as exc:
            return _error("unavailable", f"failed to spawn grok: {exc}", retryable=True)

        done = threading.Event()

        def _cancel_process() -> None:
            while not done.wait(0.25):
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return

        if cancel_event is not None:
            threading.Thread(target=_cancel_process, daemon=True).start()

        try:
            stdout_b, stderr_b = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)
            return _error("timeout", f"grok exceeded {timeout}s", retryable=True)
        finally:
            done.set()

        latency_ms = (time.monotonic() - started) * 1000.0
        stdout = (stdout_b or b"")[:_MAX_OUTPUT_BYTES].decode("utf-8", "replace")
        stderr = (stderr_b or b"")[:_MAX_OUTPUT_BYTES].decode("utf-8", "replace")

        if cancel_event is not None and cancel_event.is_set():
            return _error("unavailable", "Grok request cancelled", retryable=True)

        if proc.returncode != 0:
            result = _classify_cli_failure(proc.returncode, stderr, stdout)
            result["latency_ms"] = latency_ms
            return result

        parsed = _parse_json_output(stdout, expect_envelope=expect_envelope)
        if "error" in parsed:
            parsed["latency_ms"] = latency_ms
            return parsed
        parsed["latency_ms"] = latency_ms
        return parsed
    finally:
        # Full tree cleanup (CLI may create nested dirs under --cwd).
        shutil.rmtree(work, ignore_errors=True)


def _tool_max_turns(*, prompt: str, tool_count: int) -> int:
    """Grok needs >1 internal turn for JSON tool envelopes; large prompts need more."""
    if tool_count <= 0:
        return 1
    # Empirical: ~21k system + 112 slim tools needs 5+ turns; cap to limit runaway CLI cost.
    extra = min(5, len(prompt) // 8000)
    if tool_count > 80:
        extra = max(extra, 2)
    return max(3, min(8, 3 + extra))


def _tools_prompt_suffix(tools: list[Mapping[str, Any]]) -> str:
    catalog = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        name = tool.get("name")
        if not name:
            continue
        desc = str(tool.get("description") or "").strip()
        if len(desc) > 120:
            desc = desc[:117] + "..."
        catalog.append({"name": name, "description": desc})
    if not catalog:
        return ""
    must_call = (
        "You MUST call a tool from TOOLS for live data or actions — never answer from memory. "
        if len(catalog) <= 15
        else ""
    )
    return (
        "\n\nIMPORTANT: Reply with JSON only — no prose outside the object. "
        f"{must_call}"
        'To call a tool: {"type":"tool_calls","tool_calls":[{"name":"...","arguments":{...}}]}. '
        'When done: {"type":"final","text":"..."}. '
        "Use exact tool names from TOOLS below. Bernie executes tools server-side; "
        "never invent tool results.\n"
        f"TOOLS={json.dumps(catalog, separators=(',', ':'))}"
    )


def _needs_envelope_repair(result: Mapping[str, Any], *, expect_envelope: bool) -> bool:
    if not expect_envelope or "error" in result or result.get("tool_calls"):
        return False
    text = result.get("text")
    if text is None or not str(text).strip():
        return True
    return _extract_envelope_from_text(str(text)) is None


def grok_adapter(payload: Mapping[str, Any]) -> dict[str, Any]:
    """HTTP runner adapter entrypoint: normalized completion dict."""
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        return {
            "provider": "grok",
            "model": "",
            **_error("invalid-request", "model required", retryable=False),
        }
    model = model.strip()

    try:
        prompt = _prompt_from_payload(payload)
    except ValueError as exc:
        return {
            "provider": "grok",
            "model": model,
            **_error("invalid-request", str(exc), retryable=False),
        }

    tools_raw = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    output_schema = payload.get("output_schema") if isinstance(payload.get("output_schema"), dict) else None
    expect_envelope = bool(tools_raw) or output_schema is not None
    # Grok --json-schema for tool envelopes is flaky (structuredOutput often null); prompt JSON works.
    json_schema = output_schema if output_schema is not None else None
    if tools_raw:
        prompt = prompt + _tools_prompt_suffix(tools_raw)

    cancel_event = payload.get("_cancel_event")
    raw_timeout = payload.get("timeout_s")
    timeout_s = int(raw_timeout) if isinstance(raw_timeout, (int, float)) and raw_timeout > 0 else None
    tool_max_turns = _tool_max_turns(prompt=prompt, tool_count=len(tools_raw))
    result = run_grok_cli(
        model,
        prompt,
        json_schema=json_schema,
        expect_envelope=expect_envelope,
        cancel_event=cancel_event,
        timeout_s=timeout_s,
        max_turns=tool_max_turns,
    )

    # One bounded repair when schema fails or model returned prose instead of JSON envelope.
    if tools_raw or output_schema:
        needs_repair = (
            "error" in result and result["error"].get("code") == "schema"
        ) or _needs_envelope_repair(result, expect_envelope=expect_envelope)
        if needs_repair:
            repair_prompt = (
                prompt
                + "\n\nYour previous reply was not valid JSON. "
                "Respond with one JSON object only — "
                '{"type":"tool_calls","tool_calls":[{"name":"...","arguments":{...}}]} '
                'or {"type":"final","text":"..."}. No prose outside JSON.'
            )
            result = run_grok_cli(
                model,
                repair_prompt,
                json_schema=json_schema,
                expect_envelope=expect_envelope,
                cancel_event=cancel_event,
                timeout_s=timeout_s,
                max_turns=tool_max_turns,
            )

    body: dict[str, Any] = {
        "provider": "grok",
        "model": model,
        "latency_ms": result.get("latency_ms", 0.0),
    }
    if "error" in result:
        body["error"] = result["error"]
        body["text"] = None
        body["tool_calls"] = []
        body["usage"] = {"input_tokens": None, "output_tokens": None}
        return body

    body["text"] = result.get("text")
    body["tool_calls"] = result.get("tool_calls") or []
    body["usage"] = result.get("usage") or {"input_tokens": None, "output_tokens": None}
    if result.get("stop_reason") is not None:
        body["stop_reason"] = result["stop_reason"]
    return body
