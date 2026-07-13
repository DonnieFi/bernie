"""Shared aiohttp / TLS helpers for eval (shadow, judges, nightly).

Factored out of eval_service.py to avoid eval.shadow importing the full facade
(which would pull judges/HITL/nightly during shadow-only imports).
"""
import os
import ssl as _ssl
from contextlib import asynccontextmanager

import aiohttp

from http_session import get_http_session

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

_LF_PUBLIC = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
_LF_SECRET = os.environ.get("LANGFUSE_SECRET_KEY", "")
_LF_HOST = os.environ.get("LANGFUSE_HOST", "").rstrip("/")

# Shared default for judge/eval model selection (claude-* direct, else LiteLLM/Ollama)
DEFAULT_JUDGE_MODEL = "claude-sonnet-4-6"


def _ssl_for(url: str | None):
    """Return aiohttp's `ssl=` argument appropriate for the given URL.

    Mirrors `_make_client` in claude_service.py:
    - HTTP targets → None (no TLS).
    - `litellm.example.local` with Caddy root CA mounted → verifying context built from
      that CA file.
    - `litellm.example.local` without the CA mount → False (skip verification — matches
      what `_call_litellm_shadow` did historically with `ssl=False`, and
      what the rest of the LAN-internal HTTP code does).
    - Any other HTTPS → default verification (None lets aiohttp choose).
    """
    if not url or url.startswith("http://"):
        return None
    if "litellm.example.local" in url:
        if os.path.exists("/app/caddy-root.crt"):
            return _ssl.create_default_context(cafile="/app/caddy-root.crt")
        return False
    return None


@asynccontextmanager
async def _session_or_new(session: aiohttp.ClientSession | None):
    """Yield a usable session for eval HTTP calls.

    Uses the caller's session when open; otherwise the process singleton from
    get_http_session(). Does not create or close a dedicated session.
    """
    if session is not None and not session.closed:
        yield session
        return
    yield get_http_session()
