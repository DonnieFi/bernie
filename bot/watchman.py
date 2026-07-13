import asyncio
import logging
import os
import time
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional

import aiohttp
from config import config

log = logging.getLogger("bernie.watchman")

# Sentinel a draft falls back to when the local summariser is unreachable. Must
# never reach the synthesis model alone — see _build_facts_block / generate_nightly_report.
_DRAFT_UNAVAILABLE = "Local summary unavailable."


def _build_facts_block(
    errors: Dict[str, List[str]],
    bot_logs: List[str],
    remote: Dict[str, str],
    usage: Dict[str, Any],
    docker_available: bool,
    network_timeline: str = "",
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    cognitive_failures: Optional[List[dict]] = None,
) -> str:
    """Deterministic, factual summary of the gathered audit inputs.

    This is the floor of the nightly audit: no LLM in the loop, so a degraded run
    reports exactly what was (and wasn't) collected. Crucially it distinguishes
    "checked, found nothing" from "could not check" — the latter must never read
    as all-clear. Was the missing layer behind the 2026-05-25 hallucinated email.
    """
    lines: List[str] = []

    # Container error scan (Docker).
    if not docker_available:
        lines.append(
            "Container error scan: UNAVAILABLE — Docker socket not mounted in "
            "this role; container logs were not checked."
        )
    elif errors:
        lines.append(f"Container errors detected in {len(errors)} container(s):")
        for name, errs in errors.items():
            lines.append(f"  - {name}: {len(errs)} line(s)")
            for line in errs[-5:]:
                lines.append(f"      {line}")
    else:
        lines.append("Container error scan: no container errors in the last 24h.")

    # Bernie's own log (now de-duped in get_bernie_logs for repeated bursts).
    if bot_logs:
        lines.append(f"Bernie internal errors: {len(bot_logs)} line(s):")
        for line in bot_logs[-10:]:
            lines.append(f"  {line}")
    else:
        lines.append("Bernie internal errors: none in the last 24h.")

    # Cognitive worker/DB failures (higher signal for the common "Ollama no text" / dead_letter
    # bursts that previously produced dozens of near-identical ERROR lines in bot.log).
    # Uses the public get_recent_cognitive_failures; grouped for brevity in the audit email.
    if cognitive_failures:
        lines.append(f"Cognitive task failures (24h): {len(cognitive_failures)}")
        groups: dict = {}
        for f in cognitive_failures:
            key = (f.get("type", "?"), (f.get("error") or "")[:90])
            if key not in groups:
                groups[key] = {"count": 0, "latest": f.get("created_at", "")}
            groups[key]["count"] += 1
        for (typ, err), info in sorted(groups.items(), key=lambda x: -x[1]["count"])[:6]:
            c = info["count"]
            suffix = f" (latest {info['latest'][:10]})" if info.get("latest") else ""
            lines.append(f"  - {typ}: {c}x  {err}{suffix}")
    else:
        lines.append("Cognitive task failures (24h): none recorded.")

    # Remote health entities.
    if remote:
        lines.append("Remote health:")
        for eid, state in remote.items():
            lines.append(f"  - {eid}: {state}")
    else:
        lines.append("Remote health: no entities reported (check skipped or none configured).")

    # AI usage.
    if usage:
        lines.append("AI usage (24h):")
        lines.append(f"  {json.dumps(usage)}")
    else:
        lines.append("AI usage (24h): no usage data available.")

    # Tool calls.
    if tool_calls:
        import re
        tool_counts = {}
        for tc in tool_calls:
            desc = tc.get("description", "")
            m = re.search(r"<b>(\w+)</b>", desc)
            if m:
                tname = m.group(1)
                tool_counts[tname] = tool_counts.get(tname, 0) + 1
        
        if tool_counts:
            lines.append("Tool calls (24h):")
            for tname, count in sorted(tool_counts.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  - {tname}: {count}")
        else:
            lines.append("Tool calls (24h): none recorded.")
    else:
        lines.append("Tool calls (24h): none recorded.")

    if network_timeline:
        lines.append(network_timeline)
    else:
        lines.append("Overnight network: timeline unavailable (network watchman not run).")

    return "\n".join(lines)

class Watchman:
    """Nightly auditor for Bernie's household infrastructure.
    
    Uses aiohttp with UnixConnector to talk directly to the Docker API.
    """
    
    def __init__(self, bot=None, router=None, db_module=None):
        self.bot = bot
        self._router = router
        self._db = db_module
        self.socket_path = "/var/run/docker.sock"
        self._docker_available = os.path.exists(self.socket_path)
        
        if self._docker_available:
            log.info("Watchman: Docker socket found, ready for audits.")
        else:
            # Expected outside the cognition role (discord/api don't mount the
            # socket and never run the audit). Not an error — keep it off WARNING
            # so it doesn't read as a fault in the very log the audit scans.
            log.info(
                "Watchman: Docker socket not mounted — container error scan "
                "disabled for this role."
            )

    def _get_connector(self):
        return aiohttp.UnixConnector(path=self.socket_path)

    async def _docker_request(self, method: str, path: str, params: dict = None, _session: aiohttp.ClientSession = None) -> Any:
        """Helper to make a direct request to the Docker API.

        Pass _session to reuse an existing ClientSession (e.g. inside get_recent_errors).
        If omitted, a new session is created and closed for this single call.
        """
        if not self._docker_available:
            return None

        url = f"http://localhost{path}"

        async def _do(s):
            try:
                async with s.request(method, url, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        ct = resp.headers.get("Content-Type", "")
                        if "json" in ct and not path.endswith("/logs"):
                            return await resp.json()
                        return await resp.read()
                    else:
                        log.error(f"Watchman: Docker API error {resp.status} on {path}")
                        return None
            except Exception as e:
                log.error(f"Watchman: Docker connection failed: {e}")
                return None

        if _session is not None:
            return await _do(_session)
        from http_session import DEFAULT_CLIENT_TIMEOUT

        async with aiohttp.ClientSession(
            connector=self._get_connector(),
            timeout=DEFAULT_CLIENT_TIMEOUT,
        ) as session:
            return await _do(session)

    def _parse_docker_logs(self, raw_data: bytes) -> List[str]:
        """Parse Docker's multiplexed stream format into a list of strings.
        
        Format: [8 bytes header][payload]
        Header: [1 byte type (0/1/2)][3 bytes ignored][4 bytes length]
        """
        if not isinstance(raw_data, bytes):
            return []

        import struct
        lines = []
        offset = 0
        while offset < len(raw_data):
            if offset + 8 > len(raw_data): 
                break
            
            header = raw_data[offset:offset+8]
            try:
                _, length = struct.unpack(">BxxxL", header)
                payload_end = offset + 8 + length
                if payload_end > len(raw_data):
                     break
                     
                payload = raw_data[offset+8 : payload_end]
                line = payload.decode("utf-8", errors="ignore").strip()
                if line:
                    lines.append(line)
                offset = payload_end
            except Exception:
                # If frame is malformed, skip to next likely boundary
                offset += 1
        return lines

    async def get_recent_errors(self, hours: int = 24) -> Dict[str, List[str]]:
        """Collect error/critical logs from local containers using a single Docker session."""
        wm_cfg = config.get("watchman", {})
        monitored = wm_cfg.get("monitored_containers", [])
        since_ts = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
        keywords = ["ERROR", "CRITICAL", "Exception", "Traceback", "failed", "unhealthy"]

        results = {}
        from http_session import DEFAULT_CLIENT_TIMEOUT

        async with aiohttp.ClientSession(
            connector=self._get_connector(),
            timeout=DEFAULT_CLIENT_TIMEOUT,
        ) as session:
            containers = await self._docker_request("GET", "/containers/json", {"all": "1"}, _session=session)
            if not containers:
                return {}

            for c in containers:
                c_name = c["Names"][0].lstrip("/")
                if monitored and c_name not in monitored:
                    continue

                raw_data = await self._docker_request("GET", f"/containers/{c_name}/logs", {
                    "since": str(since_ts),
                    "stderr": "1",
                    "stdout": "1",
                    "tail": "100"
                }, _session=session)

                all_lines = self._parse_docker_logs(raw_data)
                found = [l for l in all_lines if any(k in l for k in keywords)]
                if found:
                    results[c_name] = found[-50:]

        return results

    async def get_bernie_logs(self, hours: int = 24) -> List[str]:
        """Read Bernie's own bot.log file, filtered to the last `hours` window."""
        log_path = "/data/bot.log"
        if not os.path.exists(log_path):
            return []

        since_dt = datetime.now() - timedelta(hours=hours)
        # Level-aware filter: only flag genuine ERROR/CRITICAL lines plus a
        # short list of content phrases that indicate real faults. The old
        # filter matched the substring "failed" anywhere — which caught
        # operational degradation lines like "Jina fetch failed <url>" at
        # WARNING level and surfaced them as "Bernie internal errors" in the
        # nightly audit. Log format: "YYYY-MM-DD HH:MM:SS,fff [LEVEL] module: msg"
        error_levels = ("[ERROR]", "[CRITICAL]")
        signal_phrases = ("Traceback", "Interaction expired")

        def _read():
            from collections import deque, defaultdict
            import re
            found = []
            try:
                with open(log_path, "r") as f:
                    for line in deque(f, maxlen=500):
                        if not (
                            any(lvl in line for lvl in error_levels)
                            or any(p in line for p in signal_phrases)
                        ):
                            continue
                        # Log format: "2026-05-10 20:30:20,534 [INFO] ..."
                        try:
                            ts = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
                            if ts < since_dt:
                                continue
                        except (ValueError, IndexError):
                            pass  # Unparseable timestamp — include the line
                        found.append(line.strip())
                # Slightly heavier hygiene: de-dupe repeated/near-identical errors in the window
                # (e.g. the burst of 20x "CognitiveWorker: task id=225 failed: StudyGuide: Ollama returned no text").
                # This stops the facts block + audit report from spamming 10 near-dupe lines for one incident.
                # Normalize by stripping ts + collapsing task ids / large numbers so "N x identical pattern" surfaces.
                norm_groups = defaultdict(list)
                for line in found:
                    core = re.sub(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ', '', line)
                    core = re.sub(r'task id=\d+', 'task id=*', core)
                    core = re.sub(r'\bid=\d+', 'id=*', core)
                    core = re.sub(r'\d{3,}', 'N', core)
                    norm_groups[core].append(line)
                deduped = []
                for core, orig_lines in sorted(norm_groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                    cnt = len(orig_lines)
                    example = orig_lines[-1]  # most recent representative
                    if cnt > 1:
                        deduped.append(f"[{cnt}x] {example}")
                    else:
                        deduped.append(example)
                return deduped[-50:]
            except Exception as e:
                log.error(f"Watchman: Could not read bot.log: {e}")
                return []

        return await asyncio.to_thread(_read)

    async def get_remote_health(self) -> Dict[str, str]:
        """Check Pi-hole status on remote hosts via HA entities."""
        from ha_service import ha_service
        results = {}
        
        wm_cfg = config.get("watchman", {})
        candidates = wm_cfg.get("health_entities", ["binary_sensor.pihole_aka_status", "binary_sensor.pihole_suji_status"])

        if not candidates:
            log.info("Watchman: No health_entities configured — skipping remote health check")
            return {}

        for eid in candidates:
            state = await ha_service.get_state(eid)
            if state and state.get("state"):
                raw = state["state"]
                if raw == "on":
                    results[eid] = "Healthy"
                elif raw == "off":
                    results[eid] = "Offline"
                else:
                    results[eid] = raw.title()
            else:
                results[eid] = "skipped (entity not found in HA)"
        return results

    async def get_llm_usage_summary(self, hours: int = 24) -> Dict[str, Any]:
        """Fetch token usage metrics from local database."""
        if self._db is None:
            return {}
        try:
            return await self._db.get_token_usage_summary(hours=hours)
        except Exception as e:
            log.warning(f"Watchman: Could not fetch token usage: {e}")
            return {}

    async def generate_nightly_report(self) -> str:
        """Collect all data and use the Conductor pattern to summarize.

        The structured facts block is the floor: it always reflects exactly what
        was gathered. The local Ollama draft and the frontier synthesis are
        enrichment on top of those facts — never a substitute. If the draft
        fails we still hand the real facts to synthesis (not a placeholder); if
        synthesis fails we email the deterministic facts block. This stops a
        summariser confabulating an outage out of missing data (root cause of
        the 2026-05-25 misleading "unable to complete audit" email).
        """
        from network_watchman import get_overnight_timeline

        since_dt = datetime.now(timezone.utc) - timedelta(hours=24)
        since_iso = since_dt.isoformat()

        tool_calls = []
        cog_fails = []
        if self._db is not None:
            try:
                tool_calls = await self._db.fetch_tool_calls_since(since_iso)
            except Exception as e:
                log.warning(f"Watchman: Could not fetch tool calls: {e}")
            try:
                cog_fails = await self._db.get_recent_cognitive_failures(hours=24)
            except Exception as e:
                log.warning(f"Watchman: Could not fetch cognitive failures: {e}")

        errors, bot_logs, remote, usage, network_timeline = await asyncio.gather(
            self.get_recent_errors(),
            self.get_bernie_logs(),
            self.get_remote_health(),
            self.get_llm_usage_summary(),
            get_overnight_timeline(hours=24),
        )

        facts = _build_facts_block(
            errors, bot_logs, remote, usage, self._docker_available, network_timeline, tool_calls=tool_calls,
            cognitive_failures=cog_fails
        )

        from llm.ollama import call_ollama

        prompt = (
            "Analyze this system audit for the last 24 hours and draft a technical "
            "summary for Bernie (the executive assistant). Identify the most critical "
            "issues, ignore routine noise. Report only on what the data shows; if a "
            "section says a check was unavailable or skipped, say so — never infer an "
            "outage from missing data.\n\n"
            f"{facts}"
        )

        draft = None
        try:
            draft = await call_ollama(
                "You are Bernie's subconscious. Be technical and precise.",
                [{"role": "user", "content": prompt}],
                config,
                None
            )
            if not draft or draft.strip() in ("", _DRAFT_UNAVAILABLE):
                draft = None
        except Exception as e:
            log.warning(f"Watchman: Ollama drafting failed: {e}")
            draft = None

        # Always synthesise from the real facts; the draft is optional enrichment.
        synthesis_input = facts if not draft else f"{facts}\n\n--- Local draft summary ---\n{draft}"

        from llm.audit import call_for_audit
        from llm.runtime import get_container
        try:
            return await call_for_audit(synthesis_input, config, get_container())
        except Exception as e:
            log.error(f"Watchman: Claude synthesis failed: {e}")
            # Deterministic factual fallback — never a content-free placeholder.
            return (
                "⚠️ Nightly audit ran in degraded mode (summary synthesis "
                f"unavailable). Raw findings:\n\n{facts}"
            )

    async def run_and_email(self):
        """Perform the audit and send the email."""
        report = await self.generate_nightly_report()
        
        if not self._router:
            log.error("Watchman: No router instance available.")
            return

        try:
            wm_cfg = config.get("watchman", {})
            recipient_name = wm_cfg.get("recipient") or config.get("watchman_recipient")
            from constants import registry
            admin_id = None

            if recipient_name:
                person = registry.get(registry.resolve(recipient_name))
                if person:
                    admin_id = person.get("discord_id")
            
            # Fallback: Find the first admin
            if not admin_id:
                for p in registry.all():
                    if p.get("role") == "admin" and p.get("discord_id"):
                        admin_id = p.get("discord_id")
                        break

            if admin_id:
                await self._router.notify(self._router.notification(
                    recipient_id=str(admin_id),
                    title=f"🏡 Bernie Nightly Audit - {datetime.now().strftime('%Y-%m-%d')}",
                    message=report,
                    channels=["email"]
                ))
                log.info("Watchman: Nightly audit emailed successfully.")
            else:
                log.error("Watchman: Could not resolve a valid discord_id for any admin recipient.")
        except Exception as e:
            log.error(f"Watchman: Failed to send audit email: {e}")

# Singleton
watchman: Optional[Watchman] = None

def init_watchman(bot=None, router=None, db_module=None) -> Watchman:
    global watchman
    watchman = Watchman(bot, router, db_module=db_module)
    return watchman

def get_watchman() -> Watchman:
    if watchman is None:
        raise RuntimeError("Watchman not initialized.")
    return watchman
