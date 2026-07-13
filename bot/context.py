"""BernieContext — unified system prompt + worker observation assembly."""
from __future__ import annotations

import pathlib
from datetime import tzinfo
from zoneinfo import ZoneInfo
from config import DOCS_ROOT
from constants import registry as person_registry
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from modes import resolve_mode, load_all_modes, get_mode_override
from notification_router import _is_quiet_hours

log = logging.getLogger(__name__)

# family-bot-2wh.9: invariant rules live in the CACHED static block (not dynamic every turn)
_STATIC_INVARIANT_RULES = """
Formatting rules:
- Discord messages and DMs: full markdown OK — bold (**), italics (*), inline code (`), links ([text](url)), lists
- Web UI chat: same as Discord — full markdown renders
- send_email: plain text only in the body — no asterisks, brackets, or markdown of any kind; family addresses only (from family_members); kids post to #smithy for parent approval
- Email digests: get_recent_email_signals for "anything from school/mail lately"; read_email_message (parents only) for full body by gmail_id

Rules:
- Lead with what matters most.
- Never repeat information that's obvious from the embed fields.
- Max 2 sentences of prose per section.
- Use Halifax colloquialisms naturally, not forced.
- If weather is severe, say so plainly first.
- You can look up past events with get_historical_events when asked about previous appointments or history.
- If you don't know a family preference, habit, or fact, call read_family_context or read_person_context before guessing.
- You have web search. Use it when asked to look something up, or when a factual/current question would otherwise make you deflect or guess. Don't deflect — search.
- Discovery before deflection: before telling someone you can't find a device, sensor, or household data, try the relevant tool (get_home_state with query=, get_network_devices). Never guess that household data doesn't exist.
- Capability vs. action: if asked whether you *have*, *can*, or *have access to* a tool or capability, answer from the tools available to you — do NOT invoke a tool just to prove it exists. Only call a tool when asked to actually use it.
- For location questions use who_is_home for quick 'is X home?' checks, or get_person_location for more detail (GPS/Maps). Always call one of these for any 'where is X?' or location query.
- RBAC/Permissions: system > admin > parents > all/kids. Bernie will politely inform the user if a tool is restricted for their role.
- You are the household administrator on the 'BernieHost' host. You have get_system_health, get_container_logs, get_network_status. Never tell the user to SSH into BernieHost to check logs themselves.
- Async work: use request_research (+ defer_response) for multi-source deep dives; web_search is for quick 1–2 source lookups only.
- Cognitive workers (Reflection, MemoryConsolidation, StudyGuide) already run on schedule — do not redo their work in chat.
- Always answer the actual question. Don't pad with unnecessary info.
""".strip()


@dataclass
class BernieContext:
    static_rules: str
    dynamic_context: str
    tomorrow_context: str | None
    routines: list[dict]
    observations: list[dict]
    mode: "ModeDefinition" | None = None  # Phase 28 Wave 2c

    @classmethod
    async def build(
        cls,
        config: dict,
        person_id: str | None,
        channel_id: str | None,
        tz,
        services,
        is_dm: bool = False,
        memory_context: str | None = None,
        live_context: dict | None = None,
        openwebui: bool = False,
        user_message: str | None = None,
        mode: "ModeDefinition" | None = None,
    ) -> "BernieContext":
        tomorrow_task = asyncio.create_task(_fetch_tomorrow_context(services.db, tz, person_id))
        routines_task = asyncio.create_task(_fetch_routines(services.db, person_id))
        observations_task = asyncio.create_task(_fetch_observations(services.db, person_id))

        # Split the system prompt building into static and dynamic parts
        # to maximize prompt caching effectiveness (family-bot-2wh.9).
        _family_name = config.get("family_name", "Example")
        _behaviour = await asyncio.to_thread(_read_behaviour_files)
        static_rules = (
            f"You are Bernie, the {_family_name} family assistant in Halifax, Nova Scotia.\n\n{_behaviour}"
            if _behaviour
            else f"You are Bernie, the {_family_name} family assistant in Halifax, Nova Scotia."
        )
        # Invariant formatting/rules live in the cached static block (not re-sent as dynamic every turn)
        static_rules = static_rules + "\n\n" + _STATIC_INVARIANT_RULES
        
        dynamic_context = await asyncio.to_thread(
            build_system_prompt,
            config=config,
            tz=tz,
            person_name=person_id,
            memory_context=memory_context or "",
            is_dm=is_dm,
            live_context=live_context,
            exclude_static=True
        )

        tomorrow_context, routines, observations = await asyncio.gather(
            tomorrow_task, routines_task, observations_task, return_exceptions=True
        )
        if isinstance(tomorrow_context, Exception):
            log.warning("BernieContext: tomorrow_context fetch failed: %s", tomorrow_context)
            tomorrow_context = None
        if isinstance(routines, Exception):
            log.warning("BernieContext: routines fetch failed: %s", routines)
            routines = []
        if isinstance(observations, Exception):
            log.warning("BernieContext: observations fetch failed: %s", observations)
            observations = []

        # ── Phase 28 Wave 2c: Mode resolution (family-bot-2wh.2) ─────────────
        # Prefer pre-resolved mode from chat(); else resolve once with real user text.
        if mode is None:
            load_all_modes()
            last_user_msg = (
                (user_message or "")
                or (live_context or {}).get("last_user_message", "")
                or ""
            )
            now = datetime.now(tz)
            quiet = _is_quiet_hours(now)
            mode = resolve_mode(
                channel=channel_id,
                person_id=person_id,
                message_text=last_user_msg,
                quiet_hours_active=quiet,
                explicit_override=get_mode_override(),
                openwebui=openwebui,
            )

        return cls(
            static_rules=static_rules,
            dynamic_context=dynamic_context,
            tomorrow_context=tomorrow_context,
            routines=routines or [],
            observations=observations or [],
            mode=mode,
        )

    def render_blocks(self, caching: bool = True) -> list[dict]:
        """
        Renders the system prompt as a list of content blocks for Anthropic caching.
        Block 1: Static rules (Soul, RBAC, formatting) - CACHED
        Block 1.5: Mode Addendum (Static mode rules) - CACHED
        Block 2: Dynamic context (Time, Weather, Presence, Events) - UNCACHED

        family-bot-5hy.10: optional per-layer char/token ceilings from
        ``config.prompt_layers`` (ceilings in approximate tokens; chars≈tokens*4).
        """
        from config import config as _cfg

        pl = _cfg.get("prompt_layers") or {}
        # Approximate tokens ≈ chars/4 (Hermes-style overhead tracking)
        def _cap(text: str, max_tokens: int | None, label: str) -> str:
            if not max_tokens or max_tokens <= 0 or not text:
                return text
            max_chars = int(max_tokens) * 4
            if len(text) <= max_chars:
                return text
            log.warning(
                "prompt_layers: truncating %s from %s→%s chars (budget %s tok)",
                label,
                len(text),
                max_chars,
                max_tokens,
            )
            return text[: max_chars - 20].rstrip() + "\n…[truncated]"

        static_text = _cap(
            self.static_rules,
            pl.get("static_max_tokens"),
            "static",
        )
        # Block 1: Static Rules (The core "Who" and "How")
        blocks = [
            {"type": "text", "text": static_text}
        ]
        
        # Block 1.5: Mode-specific prompt addendum (Static, so cache it with the rules)
        if self.mode and self.mode.prompt_addendum:
            mode_text = _cap(
                f"\n\n--- Current Mode: {self.mode.name} ---\n{self.mode.prompt_addendum}",
                pl.get("mode_max_tokens"),
                "mode",
            )
            blocks.append({
                "type": "text",
                "text": mode_text,
            })

        if caching:
            blocks[-1]["cache_control"] = {"type": "ephemeral"}

        # Block 2: Dynamic Context (The "Now")
        # We combine the builder's dynamic output with extra DB-fetched context.
        live_parts = [self.dynamic_context]
        
        if self.tomorrow_context:
            live_parts.append(f"<tomorrow>\n{self.tomorrow_context}\n</tomorrow>")

        if self.routines:
            routine_lines = "\n".join(
                f"- {r.get('person_id', 'household')}: {r.get('description', '')}"
                for r in self.routines[:10]
            )
            live_parts.append(f"<routines>\n{routine_lines}\n</routines>")

        if self.observations:
            obs_lines = "\n".join(
                f"- {o.get('person_id', '?')}: {o.get('observation', '')}"
                for o in self.observations[:10]
            )
            live_parts.append(f"<observations>\n{obs_lines}\n</observations>")

        if live_parts:
            live_text = "\n\n".join(live_parts)
            live_text = _cap(live_text, pl.get("dynamic_max_tokens"), "dynamic")
            blocks.append({"type": "text", "text": live_text})

        # Overhead tracking (chars/4 ≈ tokens): fixed layers vs dynamic
        try:
            static_c = len(static_text or "")
            mode_c = len(blocks[1]["text"]) if len(blocks) > 2 else (
                len(blocks[1]["text"]) if len(blocks) == 2 and self.mode else 0
            )
            # if mode block present, structure is [static, mode, dynamic?]
            if self.mode and self.mode.prompt_addendum and len(blocks) >= 2:
                mode_c = len(blocks[1].get("text") or "")
            else:
                mode_c = 0
            dyn_c = 0
            if live_parts:
                dyn_c = len(blocks[-1].get("text") or "")
            total = max(static_c + mode_c + dyn_c, 1)
            fixed = static_c + mode_c
            overhead_pct = 100.0 * fixed / total
            if pl.get("log_overhead", True):
                log.debug(
                    "prompt_layers: static=%s mode=%s dynamic=%s tok≈%s fixed_overhead=%.0f%%",
                    static_c // 4,
                    mode_c // 4,
                    dyn_c // 4,
                    total // 4,
                    overhead_pct,
                )
        except Exception:
            pass

        return blocks

    @property
    def allowed_domains(self) -> list[str] | None:
        """Tool domains permitted by the current mode (used for filtering).

        Delegates to tool_surface.mode_ceiling so domains.deny is applied
        (allow − deny). This fixes the pre-Phase 39 behavior where deny was ignored.
        """
        if not self.mode:
            return None
        try:
            from llm.tool_surface import mode_ceiling
            return mode_ceiling(self.mode)
        except Exception:
            # Fallback to raw allow if surface module unavailable (brownfield safety)
            dom = self.mode.domains or {}
            return dom.get("allow") or None


async def _fetch_tomorrow_context(db, tz, person_id: str | None) -> str | None:
    if db is None:
        return None
    try:
        for_date = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")
        raw = await db.get_tomorrow_context(for_date, person_id)
        if raw is None:
            return None
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            return raw.get("summary") or raw.get("context") or str(raw)
        return str(raw)
    except Exception:
        return None


async def _fetch_routines(db, person_id: str | None) -> list[dict]:
    if db is None:
        return []
    try:
        rows = await db.get_routines(person_id=person_id, min_confidence=0.6)
        return rows or []
    except Exception:
        return []


async def _fetch_observations(db, person_id: str | None) -> list[dict]:
    if db is None:
        return []
    try:
        rows = await db.get_semantic_observations(person_id=person_id, limit=10)
        return rows or []
    except Exception:
        return []


_BEHAVIOUR_CACHE: str | None = None


def invalidate_behaviour_cache() -> None:
    """Force the next render_blocks() call to re-read behaviour files from disk.

    Call on /reload so a capabilities_index.md edit takes effect immediately.
    Tests that mutate `DOCS_ROOT` or behaviour-file contents MUST call this
    in setUp/tearDown — otherwise the in-process cache leaks across cases and
    asserts will see stale content.

    Production contract: behaviour files are NOT file-watched. Edits on disk
    only take effect after `/reload` (which calls this) or a container restart.
    """
    global _BEHAVIOUR_CACHE
    _BEHAVIOUR_CACHE = None


def _read_behaviour_files() -> str:
    """Return concatenated soul/bernie/capabilities_index content.

    Result is cached in-process and only re-read after invalidate_behaviour_cache().
    Hermes frozen-snapshot pattern: stable block 1 hash across turns → max KV-cache hits.
    """
    global _BEHAVIOUR_CACHE
    if _BEHAVIOUR_CACHE is not None:
        return _BEHAVIOUR_CACHE
    parts = []
    # capabilities_index.md is a compact routing index (~5k tokens saved vs full capabilities.md).
    # Full capabilities.md is available on-disk for human reference but not loaded every turn.
    for name in ["soul.md", "bernie.md", "capabilities_index.md"]:
        p = pathlib.Path(DOCS_ROOT) / name
        if p.exists():
            parts.append(p.read_text().strip())
    _BEHAVIOUR_CACHE = "\n\n".join(parts)
    return _BEHAVIOUR_CACHE

def build_system_prompt(config: dict, tz: tzinfo, person_name: str | None = None,
                        memory_context: str = "", is_dm: bool = False,
                        live_context: dict | None = None,
                        exclude_static: bool = False) -> str:
    now_dt = datetime.now(tz)
    now_str = now_dt.strftime("%A, %B %d %Y %I:%M %p %Z")
    hour = now_dt.hour
    raw_names = [p["display"] for p in person_registry.family()]
    family_names = [
        f"{p['display']} ({p['id'].capitalize()})" if p['id'].lower() != p['display'].lower() else p['display']
        for p in person_registry.family()
    ]
    family_name = config.get("family_name", "Example")

    memory_block = f"\n{memory_context}\n" if memory_context else ""
    # family-bot-5hy.5: immutable human facts (not agent-writable)
    override_block = ""
    try:
        from memory_docs import read_user_override
        ov = read_user_override(pathlib.Path(DOCS_ROOT), config)
        if ov:
            override_block = (
                "\n## USER_OVERRIDE (immutable — human-edited; never contradict or overwrite)\n"
                f"{ov}\n"
            )
    except Exception:
        override_block = ""
    behaviour_block = ""
    if not exclude_static:
        behaviour_files = _read_behaviour_files()
        behaviour_block = f"\n{behaviour_files}\n" if behaviour_files else ""

    if hour < 9:
        tone = "morning — be warm and practical, help the family get out the door"
    elif hour < 12:
        tone = "mid-morning — helpful and efficient"
    elif hour < 17:
        tone = "afternoon — relaxed and direct"
    elif hour < 21:
        tone = "evening — wind-down mode, friendly and brief"
    else:
        tone = "night — very brief, only urgent things"

    # HA devices: prefer live registry, fall back to config
    ctx = live_context or {}
    ha_states = ctx.get("ha_states")
    if ha_states:
        ha_lines = "\n".join(
            f'  - {s["name"]} ({s["entity_id"]}) → {s["state"]}'
            for s in ha_states
            if s.get("entity_id", "").split(".")[0] in ("light", "switch", "media_player")
        )
        ha_block = f"\nSmart home devices (live states — use entity_id for control):\n{ha_lines}" if ha_lines else ""
    else:
        ha_entities = config.get("home_assistant", {}).get("entities", [])
        ha_lines = "\n".join(
            f'  - {e["name"]} → entity_id: "{e["entity_id"]}"'
            for e in ha_entities
        )
        ha_block = f"\nSmart home devices (use exact entity_id values):\n{ha_lines}" if ha_lines else ""

    # Live presence block
    presence = ctx.get("presence", {})
    if presence:
        home = [n.capitalize() for n, p in presence.items() if p.get("is_home")]
        away = [n.capitalize() for n, p in presence.items() if not p.get("is_home")]
        presence_lines = []
        if home:
            presence_lines.append(f"Home now: {', '.join(home)}")
        if away:
            presence_lines.append(f"Away: {', '.join(away)}")
        presence_block = "\nPresence:\n" + "\n".join(f"  • {l}" for l in presence_lines)
    else:
        presence_block = ""

    # Weather summary
    weather_str = ctx.get("weather", "")
    weather_block = f"\nCurrent weather: {weather_str}" if weather_str else ""

    # Schedule for the period the dashboard is showing (Today / Evening / Tomorrow)
    events_str = ctx.get("today_events", "")
    schedule_label = ctx.get("schedule_label", "Today")
    events_block = f"\n{schedule_label} schedule:\n{events_str}" if events_str else ""

    # Calendar names for natural reference
    shared = config.get("shared_calendars", [])
    cal_names = []
    for entry in shared:
        if isinstance(entry, dict) and entry.get("name"):
            aliases = entry.get("alias", [])
            alias_str = f" (also: {', '.join(aliases)})" if aliases else ""
            cal_names.append(f"  - {entry['name']}{alias_str}")
    cal_block = "\nCalendars:\n" + "\n".join(cal_names) if cal_names else ""

    intro = f"You are Bernie, the {family_name} family assistant in Halifax, Nova Scotia.\n{behaviour_block}" if not exclude_static else ""
    
    return f"""{intro}

Tone: It's {tone}. Be friendly, slightly playful, never annoying or verbose.
Halifax weather is moody — lean into that personality naturally.
Examples of good Bernie tone:
  - "Classic damp Halifax morning 🌫️"
  - "Cold one today — the kind that gets into your bones."
  - "Looks like a quiet day ahead."

Current time: {now_str}
Family members: {", ".join(family_names)}
Timezone: {config["timezone"]}
{"\nYou are in a private DM. You ARE talking to the person directly — do not offer to ping or message them, do not say you can't process mentions. Just respond conversationally as if texting them 1-on-1. Never use notify_family_member in a DM.\n" if is_dm else ""}
{override_block}{memory_block}{presence_block}{weather_block}{events_block}{ha_block}{cal_block}
{"" if exclude_static else chr(10) + _STATIC_INVARIANT_RULES + chr(10)}
When users ask informal questions, route them appropriately:
- "What's coming up today?" → get_todays_events → summarise briefly
- "Anything I'm forgetting?" → check today's events + recent context
- "What time is [event]?" → search today's events by name → return time
- "When was the last...?" → get_historical_events → search past events
- "What classes does Child1 have / what's her school day / what period / first class" → get_school_schedule
- "Homework / what's due / any tests / assignments" → get_homework (timeframe: today/tomorrow/week)
"""

def build_meal_system_prompt(config: dict, tz: tzinfo) -> str:
    now = datetime.now(tz).strftime("%A, %B %d %Y %I:%M %p %Z")
    family_name = config.get("family_name", "Example")
    return f"""You are Bernie, the {family_name} family's meal planning assistant.
You help the family decide what's for dinner (and other meals), manage the shared grocery list, and keep track of the menu history.

Current time: {now}
Timezone: {config["timezone"]}

You can:
- View the meal plan for any date range. Use this to check "what we have eaten" recently by looking at past dates.
- Set or update meals (dish + optional notes).
- Remove meals from the plan.
- Search for meal ideas and recipes using the food API tool if the family needs inspiration.
- Manage a categorized grocery list (Add, Remove, View). Categories like "Produce", "Dairy", "Meat", "Frozen", "Pantry" are preferred.

Keep it casual and helpful. This is the #furnace channel where the family hangs out and plans food.
- When someone suggests a meal, confirm you've added it to the plan.
- If they ask for ideas, use your search tool or suggest family favorites.
- When someone says "add [item] to groceries", use the add_grocery_item tool. If they don't specify a category, take a best guess or put it in "Other".
- If the family is deciding on dinner, proactively check the last 7-14 days of history (using get_meals with past dates) to ensure you aren't suggesting something they just had."""

@dataclass
class MealContext:
    system_prompt: str

    @classmethod
    def build(cls, config: dict) -> "MealContext":
        # build_meal_system_prompt is CPU-only (no file I/O) so we run it
        # synchronously on the event loop. If behaviour-file loading or other
        # disk I/O is added later, wrap this in asyncio.to_thread (mirror
        # BernieContext.build) to avoid blocking chat turns.
        tz = ZoneInfo(config.get("timezone", "America/Halifax"))
        prompt = build_meal_system_prompt(config, tz)
        return cls(system_prompt=prompt)

    def render_blocks(self, caching: bool = True) -> list[dict]:
        blocks = [{"type": "text", "text": self.system_prompt}]
        if caching:
            blocks[0]["cache_control"] = {"type": "ephemeral"}
        return blocks
