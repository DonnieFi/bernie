"""Network Watchman — critical host IP tracking and UniFi infra health.

Tier A: IP registry, UniFi AP status, overnight timeline for Watchman.
Tier B: HTTP probes (IP-based), HA Pi-hole cross-check, wifi-on-server policy.

.lan DNS resolution is unreliable from the Bernie container today; probes use
literal IPs. Optional caddyfile_path parses reverse_proxy targets for stale IPs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from http_session import get_http_session

from db_binding import get_database
from config import config
import db_writes

log = logging.getLogger("bernie.network_watchman")

# Event types stored in network_events.event_type
EVENT_IP_CHANGE = "ip_change"
EVENT_IP_UNEXPECTED = "ip_unexpected"
EVENT_WIFI_ON_SERVER = "wifi_on_server"
EVENT_PATH_CHANGE = "path_change"
EVENT_AP_OFFLINE = "ap_offline"
EVENT_WIFI_CLIENT_DROP = "wifi_client_drop"
EVENT_PROBE_FAILED = "probe_failed"
EVENT_PROBE_RECOVERED = "probe_recovered"
EVENT_PIHOLE_HA_MISMATCH = "pihole_ha_mismatch"
EVENT_CADDY_STALE = "caddy_stale"

_META_WIFI = "_meta:wifi_clients"
_META_PROBE = "_meta:probe:"
_META_CADDY = "_meta:caddy_check"
# Meta rows reuse host_ip_snapshots.ip as a string value bucket (e.g. wifi client
# count, caddy check timestamp). host_id is the meta key; ip holds the payload.


def _cfg() -> dict:
    return config.get("network_watchman") or {}


def _enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def _critical_hosts() -> dict[str, dict]:
    return _cfg().get("critical_hosts") or {}


def _allowed_ips(host_cfg: dict) -> set[str]:
    ips: set[str] = set()
    for key in ("ips", "alt_ips"):
        for ip in host_cfg.get(key) or []:
            if ip:
                ips.add(str(ip).strip())
    return ips


def _name_matches(val: str, name: str) -> bool:
    """Match a device field value against a configured host name."""
    if not val or not name:
        return False
    if val == name:
        return True
    if val.startswith(f"{name}.") or val.startswith(f"{name}-"):
        return True
    if val.endswith(f"-{name}") or val.endswith(f".{name}"):
        return True
    # Longer names: configured name may appear inside device hostname
    # (e.g. "homeassistant" in "homeassistant.local") — never val-in-name
    # (which would match device "home" against name "homeassistant").
    if len(name) > 3 and name in val:
        return True
    return False


def _host_matches(device: dict, host_id: str, host_cfg: dict) -> bool:
    names = {host_id.lower()}
    for n in host_cfg.get("match_names") or []:
        names.add(str(n).lower())
    allowed = _allowed_ips(host_cfg)
    ip = str(device.get("ip") or "")
    if ip and ip in allowed:
        return True
    for field in ("display_name", "custom_name", "unifi_name", "hostname"):
        val = str(device.get(field) or "").lower().strip()
        if not val:
            continue
        for n in names:
            if _name_matches(val, n):
                return True
    return False


def _find_host_device(host_id: str, host_cfg: dict, devices: list[dict]) -> dict | None:
    matches = [d for d in devices if _host_matches(d, host_id, host_cfg)]
    if not matches:
        return None
    allowed = _allowed_ips(host_cfg)

    def _rank(d: dict) -> tuple:
        ip = str(d.get("ip") or "")
        ip_ok = 0 if ip in allowed else 1
        active = 0 if d.get("is_active") else 1
        has_ip = 0 if ip else 1
        wired_pref = 0 if d.get("is_wired") else 1
        return (ip_ok, active, has_ip, wired_pref, -(d.get("last_seen") or 0))

    matches.sort(key=_rank)
    return matches[0]


async def _fetch_unifi_infra() -> dict[str, Any]:
    """AP offline list + client counts (standalone; prefer shared snapshot in poll)."""
    from network_service import network_service

    snap = await network_service.fetch_unifi_snapshot()
    return network_service.infra_from_unifi_snapshot(snap)


async def _probe_url(name: str, url: str) -> bool:
    try:
        session = get_http_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5), ssl=False) as resp:
                return resp.status < 500
    except Exception:
        return False


def _parse_caddy_ips(path: str) -> dict[str, set[str]]:
    """Extract literal IP upstreams from a Caddyfile (best-effort)."""
    result: dict[str, set[str]] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("network_watchman: cannot read caddyfile %s: %s", path, e)
        return result
    # reverse_proxy HA or searx example IPs (see config)
    ip_re = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?\b")
    block: str | None = None
    depth = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        site = re.match(r"^([a-z0-9._-]+\.lan)\s*\{", stripped, re.I)
        if site:
            block = site.group(1).lower()
            result.setdefault(block, set())
            depth = stripped.count("{") - stripped.count("}")
            continue
        if block is not None:
            depth += stripped.count("{") - stripped.count("}")
            for ip in ip_re.findall(stripped):
                result[block].add(ip)
            if depth <= 0:
                block = None
                depth = 0
    return result


def _check_caddy_stale(caddy_map: dict[str, set[str]], host_ips: dict[str, str]) -> list[dict]:
    """Flag Caddy upstream IPs that don't match any configured critical host IP."""
    all_expected = set()
    for hid, cfg in _critical_hosts().items():
        all_expected |= _allowed_ips(cfg)
        if hid in host_ips and host_ips[hid]:
            all_expected.add(host_ips[hid])

    events: list[dict] = []
    for site, upstreams in caddy_map.items():
        for ip in upstreams:
            if ip not in all_expected:
                events.append({
                    "event_type": EVENT_CADDY_STALE,
                    "host_id": site,
                    "severity": "warn",
                    "summary": f"Caddy {site} → {ip} not in critical_hosts registry",
                    "details": json.dumps({"site": site, "upstream_ip": ip}),
                })
    return events


async def _caddy_check_due(interval_hours: int = 24) -> bool:
    """True when the Caddyfile stale-IP scan should run (default: once per day)."""
    db = get_database()
    prev = await db.get_host_ip_snapshot(_META_CADDY)
    if not prev or not prev.get("updated_at"):
        return True
    try:
        last = datetime.fromisoformat(str(prev["updated_at"]).replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last >= timedelta(hours=interval_hours)


async def _mark_caddy_checked() -> None:
    db = get_database()
    await db_writes.routed("upsert_host_ip_snapshot", _META_CADDY, "ok", True, "", is_online=True)


def _classify_ip_change(host_id: str, host_cfg: dict, old_ip: str, new_ip: str) -> dict:
    allowed = _allowed_ips(host_cfg)
    if new_ip in allowed:
        return {
            "event_type": EVENT_IP_CHANGE,
            "host_id": host_id,
            "severity": "info",
            "summary": f"{host_id} IP {old_ip} → {new_ip} (within allowed set)",
            "details": json.dumps({"old_ip": old_ip, "new_ip": new_ip}),
        }
    return {
        "event_type": EVENT_IP_UNEXPECTED,
        "host_id": host_id,
        "severity": "critical" if host_cfg.get("services") else "warn",
        "summary": f"{host_id} IP {old_ip} → {new_ip} (unexpected — expected {sorted(allowed)})",
        "details": json.dumps({"old_ip": old_ip, "new_ip": new_ip, "expected": sorted(allowed)}),
    }


async def _check_pihole_ha(host_id: str, host_cfg: dict, live_ip: str) -> dict | None:
    entity = host_cfg.get("ha_pihole_entity")
    if not entity or not live_ip:
        return None
    from ha_service import ha_service
    state = await ha_service.get_state(entity)
    if not state:
        return None
    ha_state = str(state.get("state") or "").lower()
    if ha_state == "on" and live_ip not in _allowed_ips(host_cfg):
        return {
            "event_type": EVENT_PIHOLE_HA_MISMATCH,
            "host_id": host_id,
            "severity": "critical",
            "summary": (
                f"{host_id} Pi-hole HA sensor reports healthy but IP is {live_ip} "
                f"(expected {sorted(_allowed_ips(host_cfg))})"
            ),
            "details": json.dumps({"entity": entity, "ha_state": ha_state, "live_ip": live_ip}),
        }
    return None


async def poll() -> list[dict]:
    """Run one network watchman cycle. Returns new events recorded."""
    if not _enabled():
        return []

    db = get_database()
    await db_writes.routed("ensure_network_watchman_schema", )
    from network_service import network_service
    # family-bot-1bf.3: one UniFi snapshot (alluser+sta+device) shared by devices + infra
    unifi_snap = await network_service.fetch_unifi_snapshot()
    devices = await network_service.get_devices(unifi_snapshot=unifi_snap)
    events: list[dict] = []
    host_ips: dict[str, str] = {}

    for host_id, host_cfg in _critical_hosts().items():
        device = _find_host_device(host_id, host_cfg, devices)
        prev = await db.get_host_ip_snapshot(host_id)

        if not device or not device.get("ip"):
            if prev and prev.get("is_online"):
                events.append({
                    "event_type": EVENT_PATH_CHANGE,
                    "host_id": host_id,
                    "severity": "warn",
                    "summary": f"{host_id} offline (was {prev.get('ip')})",
                    "details": json.dumps({"last_ip": prev.get("ip")}),
                })
                await db_writes.routed("upsert_host_ip_snapshot", 
                    host_id, prev.get("ip") or "", prev.get("is_wired", False),
                    prev.get("essid"), is_online=False,
                )
            elif prev is None:
                # First poll offline — still write a row so the host is visible in DB
                await db_writes.routed(
                    "upsert_host_ip_snapshot",
                    host_id, "", False, "", is_online=False,
                )
            continue

        ip = str(device["ip"])
        is_wired = bool(device.get("is_wired"))
        essid = str(device.get("essid") or "")
        is_online = bool(device.get("is_active"))
        host_ips[host_id] = ip

        if prev:
            old_ip = str(prev.get("ip") or "")
            if old_ip and old_ip != ip:
                events.append(_classify_ip_change(host_id, host_cfg, old_ip, ip))
            old_wired = bool(prev.get("is_wired"))
            if old_wired != is_wired or (prev.get("essid") or "") != essid:
                if not is_wired and host_cfg.get("warn_wifi"):
                    events.append({
                        "event_type": EVENT_WIFI_ON_SERVER,
                        "host_id": host_id,
                        "severity": "info",
                        "summary": f"{host_id} on WiFi ({essid or 'unknown SSID'}) — server policy prefers wired",
                        "details": json.dumps({"ip": ip, "essid": essid}),
                    })
                elif is_wired and not old_wired:
                    events.append({
                        "event_type": EVENT_PATH_CHANGE,
                        "host_id": host_id,
                        "severity": "info",
                        "summary": f"{host_id} moved to wired ({ip})",
                        "details": json.dumps({"ip": ip}),
                    })

        mismatch = await _check_pihole_ha(host_id, host_cfg, ip)
        if mismatch:
            events.append(mismatch)

        await db_writes.routed("upsert_host_ip_snapshot", host_id, ip, is_wired, essid, is_online=is_online)

    # UniFi infra from same snapshot (no second stat/sta)
    infra = network_service.infra_from_unifi_snapshot(unifi_snap)
    prev_wifi = await db.get_host_ip_snapshot(_META_WIFI)
    wifi_count = infra.get("wifi_clients")
    if infra.get("sta_available") and wifi_count is not None:
        if prev_wifi and prev_wifi.get("ip"):
            try:
                old_count = int(prev_wifi["ip"])
                drop = old_count - wifi_count
                if old_count >= 3 and drop >= max(3, old_count // 2):
                    events.append({
                        "event_type": EVENT_WIFI_CLIENT_DROP,
                        "host_id": None,
                        "severity": "warn",
                        "summary": (
                            f"WiFi clients dropped {old_count} → {wifi_count} "
                            f"(wired: {infra.get('wired_clients', 0)} stable)"
                        ),
                        "details": json.dumps(infra),
                    })
            except ValueError:
                pass
        await db_writes.routed("upsert_host_ip_snapshot", _META_WIFI, str(wifi_count), True, "", is_online=True)

    for ap_name in infra.get("offline_aps") or []:
        events.append({
            "event_type": EVENT_AP_OFFLINE,
            "host_id": ap_name,
            "severity": "warn",
            "summary": f"UniFi AP offline: {ap_name}",
            "details": None,
        })

    # HTTP probes (IP-based — .lan DNS unreliable from container)
    for probe_name, url in (_cfg().get("probe_urls") or {}).items():
        ok = await _probe_url(probe_name, url)
        meta_key = f"{_META_PROBE}{probe_name}"
        prev_probe = await db.get_host_ip_snapshot(meta_key)
        was_ok = (prev_probe or {}).get("essid") == "ok"
        await db_writes.routed("upsert_host_ip_snapshot", 
            meta_key, urlparse(url).hostname or probe_name, True,
            "ok" if ok else "fail", is_online=ok,
        )
        if not ok and was_ok:
            events.append({
                "event_type": EVENT_PROBE_FAILED,
                "host_id": probe_name,
                "severity": "critical" if "pihole" in probe_name.lower() else "warn",
                "summary": f"Probe failed: {probe_name} ({url})",
                "details": json.dumps({"url": url}),
            })
        elif ok and prev_probe and prev_probe.get("essid") == "fail":
            events.append({
                "event_type": EVENT_PROBE_RECOVERED,
                "host_id": probe_name,
                "severity": "info",
                "summary": f"Probe recovered: {probe_name}",
                "details": json.dumps({"url": url}),
            })

    # Optional Caddyfile stale-IP check (daily — noisy if run every poll)
    caddy_path = _cfg().get("caddyfile_path")
    interval_hours = int(_cfg().get("caddy_check_interval_hours", 24))
    if caddy_path and await _caddy_check_due(interval_hours):
        events.extend(_check_caddy_stale(_parse_caddy_ips(caddy_path), host_ips))
        await _mark_caddy_checked()

    recorded: list[dict] = []
    for ev in events:
        row = await db_writes.routed("record_network_event", 
            ev["event_type"], ev["summary"],
            host_id=ev.get("host_id"),
            severity=ev.get("severity", "info"),
            details=ev.get("details"),
        )
        recorded.append(row)
        log.info("network_watchman: [%s] %s", ev.get("severity"), ev["summary"])

    return recorded


def format_overnight_timeline(events: list[dict]) -> str:
    if not events:
        return "Overnight network: no events recorded."
    lines = ["Overnight network events:"]
    for ev in events:
        ts = (ev.get("created_at") or "")[:16].replace("T", " ")
        sev = ev.get("severity", "info")
        mark = {"critical": "!", "warn": "?", "info": "·"}.get(sev, "·")
        lines.append(f"  {mark} {ts}  {ev.get('summary', '')}")
    return "\n".join(lines)


async def get_overnight_timeline(hours: int = 24) -> str:
    db = get_database()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    events = await db.list_network_events(since=since)
    return format_overnight_timeline(events)


def format_host_snapshot(host_id: str, snap: dict | None, host_cfg: dict) -> str:
    if not snap:
        return f"  {host_id}: not seen"
    ip = snap.get("ip") or "?"
    allowed = _allowed_ips(host_cfg)
    ip_flag = ""
    if allowed and ip not in allowed:
        ip_flag = " ⚠ unexpected"
    elif allowed and ip in allowed:
        ip_flag = " ✓"
    path = "wired" if snap.get("is_wired") else "wifi"
    essid = snap.get("essid") or ""
    if essid:
        path = f"{path} ({essid})"
    online = "online" if snap.get("is_online") else "offline"
    return f"  {host_id}: {ip} · {path} · {online}{ip_flag}"


async def build_network_status(*, refresh: bool = False, event_hours: int = 24) -> str:
    """Human-readable network status for tools and slash commands."""
    if not _enabled():
        return "Network Watchman is disabled in config."

    db = get_database()
    await db_writes.routed("ensure_network_watchman_schema", )
    new_events: list[dict] = []
    if refresh:
        new_events = await poll()

    lines = ["**Homelab network status**"]
    if refresh:
        lines.append(f"Fresh poll completed ({len(new_events)} new event(s)).")

    hosts_cfg = _critical_hosts()
    for host_id, host_cfg in hosts_cfg.items():
        snap = await db.get_host_ip_snapshot(host_id)
        lines.append(format_host_snapshot(host_id, snap, host_cfg))

    wifi_meta = await db.get_host_ip_snapshot(_META_WIFI)
    if wifi_meta:
        lines.append(f"  WiFi clients (UniFi): {wifi_meta.get('ip', '?')}")

    since = (datetime.now(timezone.utc) - timedelta(hours=event_hours)).isoformat()
    events = await db.list_network_events(since=since, limit=50)
    lines.append("")
    lines.append(format_overnight_timeline(events))

    return "\n".join(lines)


async def _daytime_alert(events: list[dict], router) -> None:
    """DM admin for warn/critical events during waking hours."""
    from notification_router import _is_quiet_hours

    if _is_quiet_hours(datetime.now(timezone.utc)):
        return
    significant = [e for e in events if e.get("severity") in ("warn", "critical")]
    if not significant:
        return

    recipient = _cfg().get("daytime_alert_recipient") or config.get("watchman", {}).get("recipient", "dad")
    from constants import registry
    person = registry.get(registry.resolve(recipient))
    if not person or not person.get("discord_id"):
        log.warning("network_watchman: no discord_id for daytime alerts (%s)", recipient)
        return

    lines = ["**Network watchman**"] + [f"• {e['summary']}" for e in significant[:5]]
    if len(significant) > 5:
        lines.append(f"…and {len(significant) - 5} more (see nightly audit).")

    await router.notify(router.notification(
        recipient_id=str(person["discord_id"]),
        message="\n".join(lines),
        urgency="normal",
    ))


async def run_poll(router=None) -> list[dict]:
    events = await poll()
    if events and router:
        await _daytime_alert(events, router)
    return events
