"""Spoonacular recipe search."""
import logging
import os
import re

import aiohttp

log = logging.getLogger(__name__)
BASE_URL = "https://api.spoonacular.com"


def _key() -> str:
    return os.environ.get("SPOON_API_KEY", "")


async def search_meals(query: str, session: aiohttp.ClientSession) -> list[dict]:
    """Search recipes by name or ingredient. Returns up to 5 results."""
    try:
        async with session.get(
            f"{BASE_URL}/recipes/complexSearch",
            params={"query": query, "number": 5, "addRecipeInformation": "true", "apiKey": _key()},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                log.warning(f"Spoonacular returned {resp.status}")
                return []
            data = await resp.json()
        return data.get("results", [])
    except Exception as e:
        log.warning(f"Spoonacular search failed: {e}")
        return []


def format_meal(meal: dict) -> str:
    name = meal.get("title", "Unknown")
    ready_in = meal.get("readyInMinutes")
    servings = meal.get("servings")
    summary = re.sub(r"<[^>]+>", "", meal.get("summary", ""))
    if len(summary) > 300:
        summary = summary[:297] + "..."

    res = f"**{name}**"
    if ready_in or servings:
        meta = " · ".join(filter(None, [
            f"{ready_in} min" if ready_in else None,
            f"serves {servings}" if servings else None,
        ]))
        res += f" ({meta})"
    if summary:
        res += f"\n{summary}"
    return res
