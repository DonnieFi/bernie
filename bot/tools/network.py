"""Network tool handlers — UniFi speed test history."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import aiohttp

from http_session import get_http_session

from tools import ROLE_ADMIN, ROLE_ALL, tool


def _unifi_creds(config: dict) -> tuple[str, str | None]:
    host = config.get("presence", {}).get("unifi_host", "https://192.168.1.X")  # default; override in config.json
    key = os.environ.get("UNIFI_KEY")
    return host, key


async def _fetch_speedtest_history(host: str, key: str) -> list[dict]:
    url = f"{host}/proxy/network/v2/api/site/default/speedtest"
    session = get_http_session()
    async with session.get(
            url,
            headers={"x-api-key": key},
            ssl=False,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("data", [])


def _format_entry(e: dict, now: datetime) -> str:
    ts = datetime.fromtimestamp(e["time"] / 1000, tz=timezone.utc)
    age = now - ts
    if age.total_seconds() < 3600:
        age_str = f"{int(age.total_seconds() / 60)}m ago"
    elif age.total_seconds() < 86400:
        age_str = f"{int(age.total_seconds() / 3600)}h ago"
    else:
        age_str = f"{age.days}d ago"
    dl = e.get("download_mbps", 0)
    ul = e.get("upload_mbps", 0)
    lat = e.get("latency_ms", 0)
    if dl == 0 and ul == 0:
        return f"{age_str}: test failed"
    return f"{age_str}: ↓{dl} ↑{ul} Mbps, {lat}ms latency"


@tool(
    name="get_network_speedtest",
    description=(
        "Get internet speed test results from the UniFi router's WAN tests. "
        "Returns the latest result by default; pass days=7 for a week of history. "
        "Use for any question about internet speed, bandwidth, or network performance."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Days of history to return (1–30). Default 1 = most recent only.",
                "default": 1,
            }
        },
        "required": [],
    },
    role_required=ROLE_ALL,
    domain="network",
    tier=1,
)
async def handle_get_network_speedtest(args: dict, ctx) -> str:
    host, key = _unifi_creds(ctx.config)
    if not key:
        return "UniFi API key not configured (UNIFI_KEY env var missing)."

    days = max(1, min(30, int(args.get("days", 1))))

    try:
        entries = await _fetch_speedtest_history(host, key)
    except Exception as e:
        return f"Failed to reach UniFi: {e}"

    if not entries:
        return "No speed test history available."

    now = datetime.now(timezone.utc)
    cutoff_ms = (now - timedelta(days=days)).timestamp() * 1000
    recent = [e for e in entries if e.get("time", 0) >= cutoff_ms]
    if not recent:
        recent = [entries[-1]]

    recent.sort(key=lambda e: e["time"], reverse=True)
    return "\n".join(_format_entry(e, now) for e in recent)


@tool(
    name="get_network_status",
    description=(
        "Homelab network watchman: critical server IPs (aka, bernie-host, yanagiba, deba, ha), "
        "UniFi WiFi client count, and recent network events (IP changes, AP offline, "
        "probe failures). Use for 'check homelab IPs', 'did any server IP change', "
        "'network status', or Pi-hole/path issues after UniFi outages. "
        "Set refresh=true to poll UniFi now instead of showing last snapshot."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "refresh": {
                "type": "boolean",
                "description": "Run a fresh UniFi poll before reporting (default false).",
                "default": False,
            },
            "event_hours": {
                "type": "integer",
                "description": "Hours of event history to include (default 24).",
                "default": 24,
            },
        },
        "required": [],
    },
    role_required=ROLE_ADMIN,
    domain="network",
    tier=1,
)
async def handle_get_network_status(args: dict, ctx) -> str:
    from network_watchman import build_network_status
    refresh = bool(args.get("refresh", False))
    hours = max(1, min(168, int(args.get("event_hours", 24))))
    return await build_network_status(refresh=refresh, event_hours=hours)
