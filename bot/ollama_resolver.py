"""Resolve the live Ollama base URL across candidate hosts.

Deba is multi-homed — a dock/ethernet NIC and wifi, each with its own fixed IP
(sometimes 3 active at once), so which IP answers depends on what's connected. A
single hardcoded `ollama_base_url` fails whenever that interface is down. This
resolver probes a candidate list (`GET /api/tags`) in list order and caches the
first reachable host, so wired can be preferred over wifi simply by ordering.

- Async hot paths call `await resolve_ollama_base_url(config)` — probes (cached
  for `_TTL_S`) and returns a live host.
- Sync callers call `current_ollama_base_url(config)` — last-known-good, no probe.

Candidate order comes from `config["ollama_base_urls"]` (list); if absent it
falls back to the single `config["ollama_base_url"]`. List the per-interface IPs
wired-first; the probe routes around whichever NIC is currently down.
"""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

log = logging.getLogger("bernie.ollama_resolver")

_DEFAULT = "http://192.168.1.X:11434"  # placeholder; override via config.json ollama_base_url / ollama_base_urls
_TTL_S = 60.0
_PROBE_TIMEOUT_S = 2.0

_resolved_url: str | None = None
_resolved_at: float = 0.0
_lock = asyncio.Lock()


def _candidates(config: dict) -> list[str]:
    """Ordered candidate base URLs. Prefer the list; fall back to the single key."""
    urls = config.get("ollama_base_urls")
    if isinstance(urls, list):
        out = [u.rstrip("/") for u in urls if isinstance(u, str) and u.strip()]
        if out:
            return out
    return [config.get("ollama_base_url", _DEFAULT).rstrip("/")]


def current_ollama_base_url(config: dict) -> str:
    """Last-known-good URL without probing. Falls back to the primary candidate.

    Ignores a cached host that is no longer in the configured candidates (guards
    against a config reload that changed the host set).
    """
    candidates = _candidates(config)
    if _resolved_url in candidates:
        return _resolved_url
    return candidates[0]


async def _probe(url: str, session: aiohttp.ClientSession | None) -> bool:
    """True if `{url}/api/tags` answers 200 within the probe timeout."""
    own = session is None
    if session is None:
        # family-bot-1bf.7/1bf.8: always attach ClientTimeout on owned sessions
        from http_session import DEFAULT_CLIENT_TIMEOUT

        s = aiohttp.ClientSession(timeout=DEFAULT_CLIENT_TIMEOUT)
    else:
        s = session
    try:
        async with s.get(
            f"{url}/api/tags",
            timeout=aiohttp.ClientTimeout(total=_PROBE_TIMEOUT_S),
        ) as resp:
            return resp.status == 200
    except Exception:
        return False
    finally:
        if own:
            await s.close()


async def resolve_ollama_base_url(
    config: dict,
    *,
    session: aiohttp.ClientSession | None = None,
    force: bool = False,
) -> str:
    """Return a reachable Ollama base URL, caching the result for `_TTL_S`.

    On a fresh, non-forced hit within the TTL, returns the cache without probing.
    If no candidate is reachable, returns the last-known-good (or primary) and
    does NOT cache the miss, so the next call re-probes.
    """
    global _resolved_url, _resolved_at
    candidates = _candidates(config)

    # Fast path: a fresh cache that is still a valid candidate. The `in
    # candidates` guard means a config reload that dropped the cached host
    # forces a re-probe instead of returning a now-invalid URL.
    if not force and _resolved_url in candidates and (time.monotonic() - _resolved_at) < _TTL_S:
        return _resolved_url

    # Single-flight: only one probe sequence runs at a time. Concurrent callers
    # wait on the lock then re-check the cache, rather than each launching its
    # own probe storm — which matters precisely when a NIC is down and every
    # probe burns the full 2s-per-host timeout.
    async with _lock:
        now = time.monotonic()
        if not force and _resolved_url in candidates and (now - _resolved_at) < _TTL_S:
            return _resolved_url

        for url in candidates:
            if await _probe(url, session):
                if url != _resolved_url:
                    log.info("Ollama resolver: live host = %s", url)
                _resolved_url, _resolved_at = url, now
                return url

        # Nothing reachable. Prefer last-known-good only if it's still a valid
        # candidate; otherwise the current primary. Don't cache the miss.
        fallback = _resolved_url if _resolved_url in candidates else candidates[0]
        log.warning(
            "Ollama resolver: no candidate reachable (%s); using %s",
            candidates, fallback,
        )
        return fallback


async def preflight_ollama(
    config: dict,
    *,
    session: aiohttp.ClientSession | None = None,
    sync_models: bool = True,
) -> dict:
    """Force-probe Ollama candidates before overnight cognitive jobs.

    Returns {"reachable": bool, "url": str, "candidates": list[str], "models": list[str]|None}.
    When sync_models=True and a host answers, refreshes config.ollama_models from /api/tags.
    """
    candidates = _candidates(config)
    url = await resolve_ollama_base_url(config, session=session, force=True)
    reachable = await _probe(url, session)
    models: list[str] | None = None

    if reachable and sync_models:
        own = session is None
        if session is None:
            from http_session import DEFAULT_CLIENT_TIMEOUT

            s = aiohttp.ClientSession(timeout=DEFAULT_CLIENT_TIMEOUT)
        else:
            s = session
        try:
            async with s.get(
                f"{url.rstrip('/')}/api/tags",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = sorted(m["name"] for m in data.get("models", []) if m.get("name"))
                    if models:
                        try:
                            from config import update_config
                            await update_config({"ollama_models": models})
                        except Exception:
                            log.exception("preflight_ollama: model list sync failed")
        except Exception:
            log.exception("preflight_ollama: /api/tags fetch failed for %s", url)
        finally:
            if own:
                await s.close()

    return {
        "reachable": reachable,
        "url": url,
        "candidates": candidates,
        "models": models,
    }


def _reset_for_tests() -> None:
    """Clear the module cache (test hook only)."""
    global _resolved_url, _resolved_at
    _resolved_url, _resolved_at = None, 0.0
