"""Daily public-IP health check → #anvil on change (family-bot-5hy.3)."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from db_binding import get_database
from http_session import get_http_session

log = logging.getLogger("bot")

METADATA_KEY = "external_public_ip"
IPIFY_URL = "https://api.ipify.org"
FETCH_TIMEOUT_S = 10


def ip_change_alert(previous: str | None, current: str | None) -> str | None:
    """Pure change detector.

    Returns an #anvil message when the public IP changed from a known previous
    value. First observation (no previous) and empty/failed fetches return None.
    """
    if not current:
        return None
    cur = current.strip()
    if not cur:
        return None
    prev = (previous or "").strip()
    if not prev:
        return None  # first store — no alert
    if prev == cur:
        return None
    return f"🌐 **Public IP changed**\n`{prev}` → `{cur}`"


async def fetch_public_ip(*, url: str = IPIFY_URL, timeout_s: float = FETCH_TIMEOUT_S) -> str | None:
    """Fetch the current public IP. Returns None on failure."""
    try:
        session = get_http_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as resp:
            if resp.status != 200:
                log.warning("external_ip: ipify status=%s", resp.status)
                return None
            text = (await resp.text()).strip()
            return text or None
    except Exception as e:
        log.warning("external_ip: fetch failed: %s", e)
        return None


async def run_external_ip_check(*, bot: Any = None, config: dict | None = None) -> dict:
    """Compare public IP to last-known; alert #anvil on change. Persist via set_db_metadata."""
    import db_writes

    cfg = config if config is not None else {}
    current = await fetch_public_ip()
    if not current:
        return {"ok": False, "reason": "fetch_failed"}

    db = get_database()
    previous = await db.get_db_metadata(METADATA_KEY)
    alert = ip_change_alert(previous, current)

    if previous != current:
        await db_writes.routed("set_db_metadata", METADATA_KEY, current)

    if alert:
        try:
            from cross_container import post_to_anvil

            await post_to_anvil(alert, bot=bot, config=cfg)
        except Exception as e:
            log.error("external_ip: #anvil post failed: %s", e, exc_info=True)
            return {"ok": True, "ip": current, "changed": True, "notified": False}
        log.info("external_ip: change notified %s → %s", previous, current)
        return {"ok": True, "ip": current, "changed": True, "notified": True}

    log.debug("external_ip: unchanged %s", current)
    return {"ok": True, "ip": current, "changed": False}


async def external_ip_check_task():
    """BTS entry — daily public IP health check."""
    from config import config

    try:
        # bot=None → post_to_anvil uses cross-container /internal/post
        await run_external_ip_check(bot=None, config=config)
    except Exception as e:
        log.error("external_ip_check_task error: %s", e, exc_info=True)
        raise
