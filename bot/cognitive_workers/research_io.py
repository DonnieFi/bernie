"""Search + fetch helpers for ResearchWorker.

SearXNG returns URLs only; Jina Reader (https://r.jina.ai/<url>) returns the
cleaned plaintext content of any URL with no API key.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List

import aiohttp

log = logging.getLogger("bernie.research.io")


async def searxng_search(
    session: aiohttp.ClientSession,
    base_url: str,
    query: str,
    limit: int = 5,
    timeout_s: int = 8,
) -> List[str]:
    """Return up to `limit` URLs from SearXNG; empty list on any failure."""
    url = f"{base_url.rstrip('/')}/search"
    try:
        async with session.get(
            url,
            params={"q": query, "format": "json"},
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            if resp.status != 200:
                log.warning("SearXNG HTTP %d for q=%r", resp.status, query)
                return []
            data = await resp.json()
    except Exception as e:
        # Operational degradation, not a bug — worker skips this search and moves on.
        # Single-line warning so the nightly audit doesn't flag it as an internal error.
        log.warning("SearXNG fetch failed q=%r: %s", query, e)
        return []
    out: List[str] = []
    for r in (data.get("results") or [])[:limit]:
        u = r.get("url")
        if u:
            out.append(u)
    return out


async def fetch_jina(
    session: aiohttp.ClientSession,
    target_url: str,
    timeout_s: int = 20,
    max_chars: int = 8000,
) -> str | None:
    """Fetch cleaned plaintext via Jina Reader; returns truncated body or None."""
    reader_url = f"https://r.jina.ai/{target_url}"
    try:
        async with session.get(reader_url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
            if resp.status != 200:
                log.warning("Jina HTTP %d for %s", resp.status, target_url)
                return None
            text = await resp.text()
    except Exception as e:
        # Operational degradation — worker skips this URL and moves on to others.
        # Single-line warning so the nightly audit doesn't flag it as an internal error.
        # If this becomes a pattern across many URLs, see the browser-fetch-upgrade
        # side quest (CDP + Chromium fallback for JS-rendered / 451 geo-blocked pages).
        log.warning("Jina fetch failed %s: %s", target_url, e)
        return None
    # family-bot-5hy.7: empty / tiny bodies burn synthesis budget — treat as failed fetch
    min_chars = 200
    body = (text or "").strip()
    if len(body) < min_chars:
        log.warning(
            "Jina empty/short body (%d chars < %d) for %s — skipping",
            len(body), min_chars, target_url,
        )
        return None
    return body[:max_chars]


async def fetch_many(
    session: aiohttp.ClientSession,
    urls: List[str],
    concurrency: int = 3,
    timeout_s: int = 20,
    max_chars_per_doc: int = 8000,
) -> List[tuple[str, str]]:
    """Concurrent Jina fetches. Returns list of (url, text) for successful fetches only."""
    sem = asyncio.Semaphore(concurrency)

    async def _one(u: str):
        async with sem:
            t = await fetch_jina(session, u, timeout_s=timeout_s, max_chars=max_chars_per_doc)
            return (u, t) if t else None

    results = await asyncio.gather(*(_one(u) for u in urls))
    return [r for r in results if r]
