"""Web search + URL fetch tool handlers."""
from __future__ import annotations

import re

import aiohttp

from tools import ROLE_ALL, tool


def _ssl_ctx():
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


@tool(
    name="fetch_url",
    description=(
        "Fetch the plain-text content of any URL — works like curl for the "
        "family. Strips HTML tags and returns up to 6000 characters."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The full URL to fetch (http:// or https://)"}
        },
        "required": ["url"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_fetch_url(args: dict, ctx) -> str:
    url = args.get("url", "")
    session = ctx.services.session
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Bernie/1.0)"}
        async with session.get(
            url,
            headers=headers,
            ssl=_ssl_ctx(),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                return f"Could not fetch page (HTTP {r.status})."
            html = await r.text()
            text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
            text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            return text[:6000] + ("…" if len(text) > 6000 else "")
    except Exception as e:
        return f"Could not fetch page: {e}"


@tool(
    name="web_search",
    description=(
        "Search the web for a quick factual lookup (1–2 sources). "
        "Do NOT use for trip planning, multi-option comparisons, or deep dives — "
        "use request_research + defer_response instead (runs on Ollama/deba)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"],
    },
    role_required=ROLE_ALL,
    tier=1,
)
async def handle_web_search(args: dict, ctx) -> str:
    searxng_url = ctx.config.get("searxng_url", "http://search.lan")
    query = args.get("query", "")
    session = ctx.services.session
    try:
        async with session.get(
            f"{searxng_url}/search",
            params={"q": query, "format": "json"},
            ssl=_ssl_ctx(),
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status == 200:
                data = await r.json()
                results = data.get("results", [])[:5]
                if not results:
                    return "No results found."
                return "\n\n".join(
                    f"Title: {res.get('title', '(no title)')}\n"
                    f"URL: {res.get('url', '')}\n"
                    f"Summary: {res.get('content', '')}"
                    for res in results
                )
            return f"Search unavailable (HTTP {r.status})."
    except Exception as e:
        return f"Search unavailable: {e}"
