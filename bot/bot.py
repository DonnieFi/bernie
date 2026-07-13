"""
Bernie — Family Schedule Discord Bot
Claude is the brain; family chats naturally in #smithy.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time as _time_mod
from collections import defaultdict
from datetime import datetime, timedelta, time, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from background_scheduler import get_scheduler, timing_snapshot
from db_binding import get_database
import db_writes
from telemetry import fire_and_forget
from calendar_service import CalendarService
from llm.chat import chat as claude_chat, chat_general as claude_chat_general
from llm.model_state import set_model, get_model_info, DEFAULT_MODEL
from llm.clients import model_cache_support
from llm.turn_timer import TurnTimer  # perf instrumentation (additive, fire-and-forget)
from config import TASK_TZ, config, reload_config, update_config
from constants import registry as person_registry, _rebuild_legacy as _rebuild_person_legacy
from garbage_service import get_tomorrow_collection, get_next_collections
from weather_service import get_weather, get_weather_week, get_weather_for_request, weather_line, weather_forecast_line
from recommendation_engine import get_recommendations
from summary_builder import build_highlights, format_highlights
from memory_service import record_acknowledged, record_missed, get_memory_context
from modes import set_mode_override, get_mode, load_all_modes, get_mode_override, resolve_mode
from notification_router import NotificationRouter
from frigate_service import frigate_service

if TYPE_CHECKING:
    from service_container import ServiceContainer

_container: ServiceContainer | None = None


def _init(container: ServiceContainer) -> None:
    global _container
    _container = container


# ── Shared Session ──────────────────────────────────────────────────────────
def get_session() -> aiohttp.ClientSession:
    if _container is None or _container.session is None or _container.session.closed:
        raise RuntimeError("Session not initialized. Ensure ServiceContainer is fully constructed.")
    return _container.session

# ── Logging ───────────────────────────────────────────────────────────────────
from config import ROOT_DIR
BOT_LOG = f"{ROOT_DIR}/data/bot.log" if ROOT_DIR == "/opt/family-bot" else "/data/bot.log"

_handlers = [logging.StreamHandler()]
try:
    # Ensure directory exists before trying to open the log file
    os.makedirs(os.path.dirname(BOT_LOG), exist_ok=True)
    _handlers.append(logging.FileHandler(BOT_LOG))
except Exception:
    pass # Fallback to stream only if path is not writable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_handlers
)
log = logging.getLogger("bernie")

# ── Startup validation ────────────────────────────────────────────────────────
_missing_env = [k for k in ("DISCORD_TOKEN", "ANTHROPIC_API_KEY", "SPOON_API_KEY") if not os.environ.get(k)]
if _missing_env and __name__ == "__main__":
    raise SystemExit(f"Missing required environment variables: {', '.join(_missing_env)}")

# ── Discord ───────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

tree = bot.tree
router = NotificationRouter(bot)


@tree.error
async def on_tree_error(interaction: discord.Interaction, error: Exception):
    if isinstance(error, discord.app_commands.errors.CommandInvokeError):
        error = error.original
    if isinstance(error, discord.errors.NotFound) and getattr(error, 'code', None) == 10062:
        log.warning(f"Interaction expired (10062) for command '{interaction.command.name if interaction.command else 'unknown'}' "
                    f"— user {interaction.user}, channel {interaction.channel_id}")
        return
    log.error(f"Unhandled tree error in '{interaction.command.name if interaction.command else 'unknown'}': {error}", exc_info=error)
    try:
        msg = "❌ Something went wrong. Try again in a moment."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass

# ── Calendar ───────────────────────────────────────────────────────────────
class _CalendarProxy:
    def __getattr__(self, name):
        if not _container or not _container.calendar:
            raise RuntimeError("CalendarService not initialized. Call _init() first.")
        return getattr(_container.calendar, name)

    def __setattr__(self, name, value):
        if not _container or not _container.calendar:
            raise RuntimeError("CalendarService not initialized. Call _init() first.")
        setattr(_container.calendar, name, value)

cal = _CalendarProxy()
TZ = ZoneInfo(config["timezone"])


# ─────────────────────────────────────────────────────────────────────────────
# CHANNEL MODES & MESSAGE COALESCING
# ─────────────────────────────────────────────────────────────────────────────
# Letta-inspired patterns: per-channel participation mode + debounce coalescing.
#
# Channel modes (config: channel_modes.<channel_id>: "open"|"listen"|"mention_only"):
#   open         — Bernie responds to all messages (default)
#   listen       — respond only when @mentioned
#   mention_only — same as listen; reserved for future passive-context-only mode
#
# Message coalescing (config: message_coalescing):
#   enabled          — bool, default false
#   debounce_seconds — float, default 4.0; wait this long after last message before processing
#   max_buffer_size  — int, default 5; flush immediately if this many messages accumulate
# ─────────────────────────────────────────────────────────────────────────────

_coalesce_buffers: dict[int, list[discord.Message]] = {}   # channel_id → buffered messages
_coalesce_timers:  dict[int, asyncio.Task] = {}            # channel_id → pending flush task


def _channel_mode(channel_id: int) -> str:
    """Return "open", "listen", or "mention_only" for this channel.

    JSON only stores string keys, so we look up by `str(channel_id)`. The
    earlier int fallback was unreachable and is gone.
    """
    modes = config.get("channel_modes", {})
    return modes.get(str(channel_id), "open")


class _CoalescedMessage:
    """Synthetic stand-in for ``discord.Message`` used by message coalescing.

    Only attributes consumed by ``_handle_message`` are wrapped. Any future
    code path that reads other ``discord.Message`` attributes (``reply``,
    ``edit``, ``delete``, ``reference``, ``created_at``…) on a coalesced
    message will ``AttributeError`` in production — extend this class or
    skip coalescing for that path.

    Required attributes today: ``channel``, ``author``, ``guild``,
    ``content``, ``attachments``, ``id``. ``id`` reuses the last source
    message's id (the coalesced message has no native Discord ID).
    """

    def __init__(self, base: "discord.Message", text: str) -> None:
        self.channel = base.channel
        self.author = base.author
        self.guild = base.guild
        self.content = text
        self.attachments: list = []
        self.id = base.id


def _bot_mentioned(message: discord.Message) -> bool:
    """True if the bot was @mentioned in this message."""
    return bot.user is not None and bot.user.mentioned_in(message)


async def _flush_coalesce_buffer(channel_id: int, handler, **kwargs) -> None:
    """Process all buffered messages for a channel as one coalesced call."""
    messages = _coalesce_buffers.pop(channel_id, [])
    _coalesce_timers.pop(channel_id, None)
    if not messages:
        return

    if len(messages) == 1:
        # Single message — process normally through the existing path
        await _handle_message(messages[0], handler, **kwargs)
        return

    # Multiple messages — combine into a single context for Bernie
    combined_text = "\n".join(
        f"[{m.author.display_name}]: {m.content}" for m in messages if m.content.strip()
    )
    if not combined_text.strip():
        return

    # Use the last message's channel/author context for delivery; fake a combined content
    last = messages[-1]
    log.info(
        "coalescing %d messages in channel %d → single call",
        len(messages), channel_id,
    )
    await _handle_message(_CoalescedMessage(last, combined_text), handler, **kwargs)


async def _maybe_coalesce(
    message: discord.Message, handler, **kwargs
) -> None:
    """Buffer a message and schedule/reset debounce flush, or pass through immediately.

    Concurrency invariant — this is intentionally single-threaded asyncio:
    the buffer mutation (``setdefault().append()``) and the timer
    cancel/reschedule happen between awaits, so no other coroutine can
    observe partial state. Do NOT refactor to use threads or
    ``asyncio.to_thread`` around this section without adding a lock — the
    in-flight ``_debounce`` task can already be past its ``sleep`` and inside
    ``_flush_coalesce_buffer`` when we cancel, and ``cancel()`` is a no-op
    at that point.
    """
    from discord_typing import typing_ack

    await typing_ack(message.channel)

    coalesce_cfg = config.get("message_coalescing", {})
    if not coalesce_cfg.get("enabled"):
        await _handle_message(message, handler, **kwargs)
        return

    debounce = float(coalesce_cfg.get("debounce_seconds", 4.0))
    max_buf = int(coalesce_cfg.get("max_buffer_size", 5))
    channel_id = message.channel.id

    _coalesce_buffers.setdefault(channel_id, []).append(message)

    # Cancel any existing flush timer for this channel. If the prior task is
    # already past its sleep and inside _flush_coalesce_buffer, cancel() is a
    # no-op — the flush will complete and pop its own buffer; our append above
    # lands in a fresh list created by the next setdefault().
    existing = _coalesce_timers.pop(channel_id, None)
    if existing and not existing.done():
        existing.cancel()

    if len(_coalesce_buffers[channel_id]) >= max_buf:
        # Buffer full — flush immediately
        await _flush_coalesce_buffer(channel_id, handler, **kwargs)
        return

    async def _debounce():
        await asyncio.sleep(debounce)
        await _flush_coalesce_buffer(channel_id, handler, **kwargs)

    _coalesce_timers[channel_id] = asyncio.create_task(_debounce())


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_OUTDOOR_KEYWORDS = {"bus", "walk", "game", "practice", "park", "outside", "outdoor",
                     "recess", "pickup", "soccer", "baseball", "track", "field"}


def _weather_nudge_text(weather: dict, events: list, tz) -> str | None:
    """Return a nudge string if weather + schedule warrants one, else None."""
    precip = weather.get("precip_prob_pct", 0) or 0
    wind = weather.get("wind_kmh", 0) or 0

    if wind >= 60:
        return f"💨 Strong winds today ({wind} km/h). Heads up for anyone heading out."

    if precip >= 50:
        for event in events:
            if event.get("all_day"):
                continue
            title = (event.get("summary") or "").lower()
            loc = (event.get("location") or "").lower()
            combined = f"{title} {loc}"
            if any(kw in combined for kw in _OUTDOOR_KEYWORDS):
                start = event["start"]
                if hasattr(start, "astimezone"):
                    start = start.astimezone(tz)
                time_str = start.strftime("%-I:%M %p")
                return (
                    f"🌧 Rain likely ({precip}%) around {time_str} "
                    f"— **{event['summary']}** may be affected."
                )
    return None


def get_schedule_channel() -> discord.TextChannel | None:
    return bot.get_channel(config["schedule_channel_id"])


def reminder_windows(event: dict) -> list[int]:
    if event.get("custom_remind"):
        return event["custom_remind"]
    return config.get("default_reminder_minutes", [15])


# ── Embed layout functions (Phase 4.2) ──────────────────────────────────────
# Implementations live in bot/ui/embeds.py; shims below capture TZ from scope.
from ui.embeds import (
    build_reminder_embed as _embed_reminder,
    build_summary_embed as _embed_summary,
    build_school_embed as _embed_school,
    build_homework_embed as _embed_homework,
    build_draft_embed,
    build_weekly_embed as _embed_weekly,
)


def build_reminder_embed(event: dict, mins_until: int) -> discord.Embed:
    return _embed_reminder(event, mins_until)


def _discord_to_person_id(discord_id: int) -> str | None:
    """Look up canonical person_id from a Discord user's ID via the registry."""
    return person_registry.resolve(discord_id)


def _person_to_discord_id(person_id: str) -> int | None:
    from task_access import person_to_discord_id
    return person_to_discord_id(person_id)


def _unified_tasks():
    return _container.unified_tasks if _container else None


async def _broadcast_task_update(action: str, task_id: int, **extra) -> None:
    if _container and _container.connection_manager:
        payload = {"type": "task.update", "action": action, "task_id": task_id}
        payload.update(extra)
        await _container.connection_manager.broadcast(payload)


from utils.discord_helpers import person_display_name, weekday_num, next_automation_run


def _resolve_person_id(raw: str) -> str | None:
    if not raw:
        return None
    return person_registry.resolve(raw)


def _now_iso() -> str:
    return datetime.now(TZ).isoformat()


# OpenRouter / GLM-style inline citation tokens leak into Discord as garbage chars
_MODEL_CITE_ARTIFACT_RE = re.compile(r"\ue200cite\ue202turn\d+search\d+\ue201", re.IGNORECASE)


def _strip_model_artifacts(text: str) -> str:
    if not text:
        return text
    cleaned = _MODEL_CITE_ARTIFACT_RE.sub("", text)
    return re.sub(r"  +", " ", cleaned).strip()


def _parse_datetime_local(raw: str) -> datetime:
    txt = (raw or "").strip()
    if not txt:
        raise ValueError("Missing datetime")
    txt = txt.replace("T", " ")
    dt = datetime.strptime(txt, "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=TZ)


def _snooze_target(preset: str) -> datetime:
    now = datetime.now(TZ)
    if preset == "15m":
        return now + timedelta(minutes=15)
    if preset == "1h":
        return now + timedelta(hours=1)
    if preset == "tomorrow":
        t = now + timedelta(days=1)
        return t.replace(hour=8, minute=0, second=0, microsecond=0)
    raise ValueError("Unknown snooze preset")


class TaskReminderView(discord.ui.View):
    """Interactive buttons attached to DM task reminders."""

    def __init__(self, task_id: int, person_id: str):
        super().__init__(timeout=86400)
        self.task_id = task_id
        self.person_id = person_id

    async def _disable(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

    async def _verify_clicker(self, interaction: discord.Interaction) -> bool:
        from task_access import person_matches
        clicker_person_id = person_registry.resolve(interaction.user.id)
        if not clicker_person_id or not person_matches(clicker_person_id, self.person_id):
            await interaction.response.send_message("❌ Only the task assignee can interact with these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success)
    async def btn_done(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._verify_clicker(interaction):
            return
        svc = _unified_tasks()
        if not svc:
            await interaction.response.send_message("❌ Task service unavailable.", ephemeral=True)
            return
        from services.unified_task_service import TaskValidationError
        try:
            updated = await svc.complete_task(
                self.task_id, actor_id=self.person_id, note="", via="dm_button",
            )
        except TaskValidationError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        if not updated:
            await interaction.response.send_message("❌ Couldn't find that task.", ephemeral=True)
            return
        await _broadcast_task_update("completed", self.task_id)
        if bool(updated.get("requires_approval")) and not updated.get("approved_at"):
            await interaction.response.send_message("✅ Marked done — waiting for approval.", ephemeral=True)
        else:
            await interaction.response.send_message("✅ Done!", ephemeral=True)
        await self._disable(interaction)

    async def _snooze_task_preset(self, interaction: discord.Interaction, preset: str, ack: str):
        if not await self._verify_clicker(interaction):
            return
        svc = _unified_tasks()
        if not svc:
            await interaction.response.send_message("❌ Task service unavailable.", ephemeral=True)
            return
        from services.unified_task_service import TaskValidationError
        until = _snooze_target(preset)
        try:
            await svc.snooze_task(
                self.task_id,
                actor_id=self.person_id,
                snooze_until=until.isoformat(),
                preset=preset,
            )
        except TaskValidationError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        await _broadcast_task_update("snoozed", self.task_id)
        await interaction.response.send_message(ack, ephemeral=True)
        await self._disable(interaction)

    @discord.ui.button(label="💤 15 min", style=discord.ButtonStyle.secondary)
    async def btn_snooze_15m(self, interaction: discord.Interaction, button: discord.ui.Button):
        until = _snooze_target("15m")
        await self._snooze_task_preset(
            interaction, "15m", f"💤 Snoozed until {until.strftime('%-I:%M %p')}",
        )

    @discord.ui.button(label="💤 1 hour", style=discord.ButtonStyle.secondary)
    async def btn_snooze_1h(self, interaction: discord.Interaction, button: discord.ui.Button):
        until = _snooze_target("1h")
        await self._snooze_task_preset(
            interaction, "1h", f"💤 Snoozed until {until.strftime('%-I:%M %p')}",
        )

    @discord.ui.button(label="💤 Tomorrow 8am", style=discord.ButtonStyle.secondary)
    async def btn_snooze_tomorrow(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._snooze_task_preset(interaction, "tomorrow", "💤 Snoozed until tomorrow 8am")





def _partition_school(events: list[dict]) -> tuple[list[dict], list[dict]]:
    from school_calendar import school_calendar_ids

    school_cals = school_calendar_ids(config)
    school = [e for e in events if e.get("calendar_id") in school_cals]
    family = [e for e in events if e.get("calendar_id") not in school_cals]
    return school, family


def build_summary_embed(events: list[dict], weather: dict | None = None, garbage: dict | None = None, prefix: str | None = None) -> discord.Embed:
    return _embed_summary(events, weather, garbage, prefix, tz=TZ)


def build_school_embed(events: list[dict]) -> discord.Embed:
    return _embed_school(events, tz=TZ)


def build_homework_embed(events: list[dict]) -> discord.Embed:
    return _embed_homework(events, tz=TZ)


def build_weekly_embed(events: list[dict], start: datetime) -> discord.Embed:
    return _embed_weekly(events, start, tz=TZ)


# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND TASKS
# ─────────────────────────────────────────────────────────────────────────────

async def _check_missed(event: dict, notified_discord_ids: list[int]) -> None:
    """Called after the missed-reminder window expires. Records any DM recipient who didn't ✅."""
    event_id = event.get("id", "")
    event_title = event.get("summary", "")
    rsvps = await get_database().get_rsvps(event_id)
    acked_ids = {r["discord_id"] for r in rsvps if r["status"] == "✅"}

    for discord_id in notified_discord_ids:
        if discord_id not in acked_ids:
            person_id = person_registry.resolve(discord_id) or str(discord_id)
            await record_missed(person_id, event_title)
            log.info(f"Recorded missed reminder: {person_id} / {event_title}")


async def check_reminders():
    try:
        now = datetime.now(TZ)
        channel = get_schedule_channel()
        if not channel:
            return

        from school_calendar import school_calendar_ids, show_school_in_daily_summary

        school_cals = school_calendar_ids(config)
        max_look = max(config.get("default_reminder_minutes", [30])) + config["poll_interval_minutes"]
        events = await cal.get_upcoming_events(lookahead_minutes=max_look)

        # family-bot-ah5.3: collect due (event, window) pairs, then batch sent-check + prefs.
        due: list[tuple[dict, int, float, bool]] = []  # event, remind_min, mins_until, is_school
        for event in events:
            if event.get("all_day"):
                continue
            start: datetime = event["start"]
            mins_until = (start - now).total_seconds() / 60
            is_school_class = event.get("calendar_id") in school_cals
            for remind_min in reminder_windows(event):
                delta = mins_until - remind_min
                if -(config["poll_interval_minutes"]) < delta <= config["poll_interval_minutes"]:
                    due.append((event, remind_min, mins_until, is_school_class))

        if not due:
            return

        db = get_database()
        unsent = await db.filter_unsent_reminders(
            [(ev["id"], rm) for ev, rm, _, _ in due]
        )
        due = [
            (ev, rm, mu, sc)
            for ev, rm, mu, sc in due
            if (str(ev["id"]), int(rm)) in unsent
        ]
        if not due:
            return

        # Prefetch prefs for all resolved attendee discord_ids (one query).
        pref_ids: list[int] = []
        for event, _rm, _mu, is_school_class in due:
            if is_school_class:
                continue
            for name in event.get("attendees", []) or []:
                person = person_registry.get(person_registry.resolve(name))
                if not person:
                    continue
                did = person.get("discord_id")
                if did and str(did) != "0":
                    pref_ids.append(int(did))
        prefs_by_id = await db.get_person_prefs_by_discord_ids(pref_ids)

        for event, remind_min, mins_until, is_school_class in due:
            if is_school_class and not show_school_in_daily_summary(config):
                await db_writes.mark_reminder_sent(event["id"], remind_min)
                continue

            embed = build_reminder_embed(event, int(mins_until))

            if is_school_class:
                # School classes: DM Child1 only — never post to channel
                child1_id = None
                person = person_registry.get(person_registry.resolve("child1"))
                if person:
                    child1_id = person.get("discord_id")
                if child1_id:
                    child1_user = bot.get_user(int(child1_id))
                    if not child1_user:
                        try:
                            child1_user = await bot.fetch_user(int(child1_id))
                        except discord.NotFound:
                            child1_user = None
                    if child1_user:
                        await router.notify(router.notification(
                            recipient_id=str(child1_id), embed=embed
                        ))
                await db_writes.mark_reminder_sent(event["id"], remind_min)
                log.info(f"School reminder → Child1 DM: {event['summary']} ({remind_min}min)")
                continue

            channel_mentions: list[str] = []
            dm_users: list[discord.User] = []

            for name in event.get("attendees", []):
                discord_id = None
                person = person_registry.get(person_registry.resolve(name))
                if person:
                    discord_id = person.get("discord_id")

                if not discord_id or str(discord_id) == "0":
                    channel_mentions.append(name)
                    continue
                prefs = prefs_by_id.get(int(discord_id)) or {
                    "reminders_enabled": True, "dm_mode": True,
                }
                if not prefs["reminders_enabled"]:
                    continue
                if prefs["dm_mode"]:
                    user = bot.get_user(int(discord_id))
                    if not user:
                        try:
                            user = await bot.fetch_user(int(discord_id))
                        except discord.NotFound:
                            log.warning(f"Could not find user {discord_id} for event {event['summary']}")
                            continue
                    if user:
                        dm_users.append(user)
                else:
                    channel_mentions.append(f"<@{discord_id}>")

            content = f"{', '.join(channel_mentions)} — heads up!" if channel_mentions else "Heads up!"

            await db_writes.add_message(
                config["schedule_channel_id"], "assistant",
                f"Reminder: {event['summary']} in {remind_min} minutes"
            )
            res = await router.notify(router.notification(
                recipient_id=str(config["schedule_channel_id"]),
                message=content,
                embed=embed
            ))
            msg = res.get("discord")
            if isinstance(msg, discord.Message):
                for emoji in ["✅", "❌", "🤔"]:
                    await msg.add_reaction(emoji)
                await db_writes.store_message_mapping(msg.id, event["id"], event["summary"])

            await db_writes.mark_reminder_sent(event["id"], remind_min)

            for user in dm_users:
                try:
                    dm_res = await router.notify(router.notification(
                        recipient_id=str(user.id), embed=embed
                    ))
                    dm_msg = dm_res.get("discord")
                    if isinstance(dm_msg, discord.Message):
                        for emoji in ["✅", "❌", "🤔"]:
                            await dm_msg.add_reaction(emoji)
                        await db_writes.store_message_mapping(dm_msg.id, event["id"], event["summary"])
                except Exception as e:
                    log.warning(f"Can't DM {user.display_name}: {e}")

            log.info(f"Reminder sent: {event['summary']} ({remind_min}min)")

            if dm_users:
                notified_ids = [u.id for u in dm_users]
                window_secs = config.get("missed_reminder_window_minutes", 60) * 60
                loop = asyncio.get_running_loop()
                loop.call_later(
                    window_secs,
                    lambda ids=notified_ids, ev=event: loop.create_task(
                        _check_missed(ev, ids)
                    )
                )
    except Exception as e:
        log.error(f"check_reminders error: {e}", exc_info=True)
        raise


async def daily_summary_task():
    try:
        now = datetime.now(TZ)
        today_key = f"summary_{now.strftime('%Y-%m-%d')}"
        if await get_database().is_reminder_sent(today_key, 0):
            return
        channel = get_schedule_channel()
        if not channel:
            return
        events = await cal.get_todays_events()
        weather = await get_weather(config.get("location", {}).get("lat", 44.6476), config.get("location", {}).get("lon", -63.5728), get_session())
        garbage = await get_tomorrow_collection(config["recollect_ics_url"], TZ, get_session()) if config.get("recollect_ics_url") else None
        ctx_prefix = None
        try:
            ctx_row = await get_database().get_tomorrow_context(now.strftime("%Y-%m-%d"), person_id=None)
            if ctx_row:
                ctx_prefix = ctx_row.get("summary")
        except Exception:
            log.exception("daily_summary: tomorrow_context lookup failed (non-fatal)")
        embed = build_summary_embed(events, weather, garbage, prefix=ctx_prefix)
        summary_text = f"Daily summary posted — {len(events)} event(s): " + ", ".join(e["summary"] for e in events[:5])
        await db_writes.add_message(config["schedule_channel_id"], "assistant", summary_text)
        await router.notify(router.notification(
            recipient_id=str(config["schedule_channel_id"]),
            embed=embed
        ))
        await db_writes.mark_reminder_sent(today_key, 0)
        log.info(f"Daily summary posted — {len(events)} events.")
    except Exception as e:
        log.error(f"daily_summary_task error: {e}", exc_info=True)
        raise


async def weekly_summary_task():
    try:
        now = datetime.now(TZ)
        if now.weekday() == 6:
            week_key = f"weekly_{now.strftime('%Y-%W')}"
            if await get_database().is_reminder_sent(week_key, 0):
                return
            channel = get_schedule_channel()
            if not channel:
                return
            await db_writes.mark_reminder_sent(week_key, 0)
            tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            events = await cal.get_events_starting(tomorrow, 7)
            embed = build_weekly_embed(events, tomorrow)
            week_text = f"Weekly summary posted — {len(events)} event(s) for the week ahead."
            await db_writes.add_message(config["schedule_channel_id"], "assistant", week_text)
            await router.notify(router.notification(
                recipient_id=str(config["schedule_channel_id"]),
                embed=embed
            ))
            log.info(f"Weekly summary posted — {len(events)} events.")
    except Exception as e:
        log.error(f"weekly_summary_task error: {e}", exc_info=True)
        raise


async def live_snapshot_task():
    """Refresh in-memory household snapshot for fast build_context reads."""
    try:
        from services.live_snapshot import refresh_live_snapshot
        session = None
        try:
            session = get_session()
        except RuntimeError:
            log.debug(
                "live_snapshot_task: no HTTP session yet; weather leg skipped",
            )
        await refresh_live_snapshot(
            config=config,
            cal_service=cal,
            session=session,
        )
        from services.live_snapshot import record_bts_refresh_success
        record_bts_refresh_success()
    except Exception as e:
        log.error("live_snapshot_task error: %s", e, exc_info=True)
        from services.live_snapshot import record_bts_refresh_failure
        record_bts_refresh_failure()


async def weather_prefetch_task():
    """Pre-warm weather cache at 5am and post weather nudges for the day."""
    try:
        lat = config.get("location", {}).get("lat", 44.6476)
        lon = config.get("location", {}).get("lon", -63.5728)
        tz_name = config.get("timezone", "America/Halifax")
        session = get_session()
        weather, _ = await asyncio.gather(
            get_weather(lat, lon, session),
            get_weather_week(lat, lon, session, tz_name=tz_name),
        )
        log.info("5am weather prefetch complete")

        if not weather:
            return
        nudge_key = f"weather_nudge_{datetime.now(TZ).strftime('%Y-%m-%d')}"
        if await get_database().is_reminder_sent(nudge_key, 0):
            return
        events = await cal.get_todays_events()
        nudge = _weather_nudge_text(weather, events, TZ)
        if nudge:
            channel = get_schedule_channel()
            if channel:
                await db_writes.add_message(config["schedule_channel_id"], "assistant", nudge)
                await router.notify(router.notification(
                    recipient_id=str(config["schedule_channel_id"]),
                    message=nudge
                ))
                await db_writes.mark_reminder_sent(nudge_key, 0)
                log.info(f"Weather nudge posted: {nudge[:60]}")
    except Exception as e:
        log.error(f"weather_prefetch_task error: {e}", exc_info=True)
        raise


async def _sync_ollama_models():
    """Query Ollama for installed models and update config.json."""
    from config import update_config
    from ollama_resolver import resolve_ollama_base_url
    ollama_url = await resolve_ollama_base_url(config, force=True)
    try:
        session = get_session()
        async with session.get(f"{ollama_url}/api/tags", timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                models = sorted(m["name"] for m in data.get("models", []))
                if models:
                    await update_config({"ollama_models": models})
                    log.info(f"Ollama model list synced: {models}")
    except Exception as e:
        log.error(f"_sync_ollama_models error: {e}")


async def litellm_model_sync_task():
    """Weekly: sync configured LiteLLM aliases from LiteLLM's DB-backed registry."""
    try:
        from litellm_service import sync_config_litellm_models

        synced = await sync_config_litellm_models()
        if synced:
            log.info("Weekly LiteLLM model sync complete: %d model(s)", len(synced))
    except Exception as e:
        log.error(f"litellm_model_sync_task error: {e}", exc_info=True)
        raise


async def ha_registry_refresh_task():
    """Daily 3am: HA entity sync. Weekly Sunday 3am: API tests + Ollama model sync."""
    try:
        from ha_service import ha_service
        await ha_service.refresh_entities()
        log.info("Daily HA entity registry refresh complete")
    except Exception as e:
        log.error(f"ha_registry_refresh_task error: {e}", exc_info=True)
        raise

    if datetime.now(TZ).weekday() != 6:  # 6 = Sunday
        return

    # Weekly API Structure Tests
    try:
        from api_tester import run_api_tests
        errors = await run_api_tests(get_session(), config)
        if errors:
            log.warning(f"Weekly API tests found {len(errors)} issues: {errors}")
            anvil_id = config.get("anvil_channel_id")
            if anvil_id:
                err_text = "\n".join(f"- {e}" for e in errors)
                msg = f"🚨 **Weekly API Test Failures**\nWe caught an upstream API change that failed structural validation:\n{err_text}"
                await router.notify(router.notification(recipient_id=str(anvil_id), message=msg))
        else:
            log.info("Weekly API tests passed successfully")
    except Exception as e:
        log.error(f"Weekly API tests error: {e}", exc_info=True)

    # Sync Ollama model list (weekly)
    try:
        await _sync_ollama_models()
    except Exception as e:
        log.error(f"ha_registry_refresh_task model sync error: {e}", exc_info=True)


async def network_watchman_task():
    """Poll critical host IPs, UniFi infra, and HTTP probes."""
    try:
        from network_watchman import run_poll
        orch = _container.notification_orchestrator if _container else None
        await run_poll(router=orch)
    except Exception as e:
        log.error(f"network_watchman_task error: {e}", exc_info=True)
        raise


async def watchman_audit_task():
    """Nightly 3am: System integrity audit and email report."""
    try:
        from watchman import get_watchman
        wm = get_watchman()
        await wm.run_and_email()
    except Exception as e:
        log.error(f"watchman_audit_task error: {e}", exc_info=True)
        raise


async def memory_prune_task():
    """Nightly 2am — delete memory_events rows older than 90 days."""
    try:
        from memory_service import prune_old_events
        deleted = await prune_old_events()
        log.info(f"memory_prune_task: pruned {deleted} old memory_events row(s)")
    except Exception as e:
        log.error(f"memory_prune_task error: {e}", exc_info=True)
        raise


async def hitl_expiry_task():
    """Expire pending HITL requests past their 5m window (every 60s)."""
    try:
        from hitl.hitl_service import run_hitl_expiry_sweep
        await run_hitl_expiry_sweep()
    except Exception as e:
        log.error(f"hitl_expiry_task error: {e}", exc_info=True)
        raise


async def proactive_nudge_task():
    """Hourly scan of routines + tomorrow_context for proactive family nudges."""
    try:
        from proactive_nudge import run_proactive_nudge_scan

        await run_proactive_nudge_scan(config, get_database(), router)
    except Exception as e:
        log.error(f"proactive_nudge_task error: {e}", exc_info=True)
        raise


async def inbox_ingest_task():
    """Hourly Gmail ingest → email_signals (Phase 34)."""
    try:
        from cognitive_workers.inbox_ingest import run_inbox_ingest
        from identity_service import identity_service

        await run_inbox_ingest(config, get_database(), identity_service)
    except Exception as e:
        log.error(f"inbox_ingest_task error: {e}", exc_info=True)
        raise


async def email_pending_expiry_task():
    """Expire kid email approvals older than 24h."""
    try:
        from email_pending_delivery import run_email_pending_expiry_sweep

        await run_email_pending_expiry_sweep(config, bot)
    except Exception as e:
        log.error(f"email_pending_expiry_task error: {e}", exc_info=True)
        raise


async def email_send_rate_prune_task():
    """Drop email_send_rate rows older than 2h."""
    try:
        from datetime import datetime, timedelta, timezone as dt_timezone

        cutoff = (datetime.now(dt_timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        removed = await db_writes.routed("prune_email_send_rate", cutoff)
        if removed:
            log.debug("email_send_rate_prune: removed %d row(s)", removed)
    except Exception as e:
        log.error(f"email_send_rate_prune_task error: {e}", exc_info=True)
        raise


async def hitl_purge_task():
    """Delete terminal pending_hitl rows older than 7 days."""
    try:
        from hitl.hitl_service import run_hitl_purge
        from llm.services import build_service_refs

        services = build_service_refs(_container)
        purged = await run_hitl_purge(services=services)
        if purged > 0:
            log.info("hitl_purge_task: purged %d terminal HITL row(s)", purged)
    except Exception as e:
        log.error(f"hitl_purge_task error: {e}", exc_info=True)
        raise


async def quiet_hours_flush_task():
    """Runs when quiet hours end — drains every recipient's overnight notification queue.

    Replaces the old arrive-home flush. flush_pending delivers as high urgency,
    so it bypasses the quiet-hours gate regardless of exact run time.
    """
    try:
        orch = _container.notification_orchestrator if _container else None
        if not orch:
            log.warning("quiet_hours_flush_task: no orchestrator available")
            return
        recipients = await get_database().list_pending_recipients()
        for rid in recipients:
            await orch.flush_pending(rid)
        if recipients:
            log.info(f"quiet_hours_flush_task: flushed {len(recipients)} recipient(s)")
    except Exception as e:
        log.error(f"quiet_hours_flush_task error: {e}", exc_info=True)
        raise


async def ollama_overnight_preflight_task():
    """Nightly 02:05 — force-probe deba Ollama before reflection/consolidation enqueue."""
    try:
        from ollama_resolver import preflight_ollama

        result = await preflight_ollama(config, sync_models=True)
        if result["reachable"]:
            model_count = len(result.get("models") or [])
            log.info(
                "ollama_overnight_preflight: live at %s (%d model(s))",
                result["url"], model_count,
            )
            return

        candidates = result.get("candidates") or []
        log.warning(
            "ollama_overnight_preflight: no Ollama host reachable (%s)",
            candidates,
        )
        anvil_id = config.get("anvil_channel_id")
        orch = _container.notification_orchestrator if _container else None
        if anvil_id and orch:
            msg = (
                "⚠️ **Ollama preflight failed** — deba unreachable before overnight "
                f"cognitive jobs.\nCandidates: {', '.join(candidates) or '(none configured)'}"
            )
            await orch.notify(orch.notification(recipient_id=str(anvil_id), message=msg, urgency="high"))
    except Exception as e:
        log.error(f"ollama_overnight_preflight_task error: {e}", exc_info=True)
        raise


async def nightly_eval_task():
    """Nightly 2:30am — score yesterday's shadow calls and post digest to #anvil."""
    try:
        from eval_service import nightly_eval_worker
        orch = _container.notification_orchestrator if _container else None
        await nightly_eval_worker(config, orchestrator=orch, bot_instance=bot)
    except Exception as e:
        log.error(f"nightly_eval_task error: {e}", exc_info=True)
        raise


async def _ollama_reachable_for_cognitive_jobs(job_name: str) -> bool:
    """Probe Ollama before enqueueing overnight cognitive work."""
    from ollama_resolver import preflight_ollama

    result = await preflight_ollama(config, sync_models=False)
    if result["reachable"]:
        return True
    candidates = result.get("candidates") or []
    log.warning(
        "%s: skipping enqueue — Ollama unreachable (%s)",
        job_name,
        candidates,
    )
    return False


async def reflection_enqueue_task():
    """Phase 26-02: nightly enqueue of a reflection cognitive_task for tomorrow_context."""
    try:
        if not await _ollama_reachable_for_cognitive_jobs("reflection_enqueue"):
            return
        tomorrow = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        tid = await db_writes.routed("create_cognitive_task", 
            type="reflection",
            payload={"for_date": tomorrow},
            priority=10,
        )
        log.info("reflection_enqueue: queued task id=%d for %s", tid, tomorrow)
    except Exception as e:
        log.error(f"reflection_enqueue_task error: {e}", exc_info=True)
        raise


async def consolidation_enqueue_task():
    """Phase 26-02: nightly enqueue of one consolidation cognitive_task per family member."""
    try:
        if not await _ollama_reachable_for_cognitive_jobs("consolidation_enqueue"):
            return
        family = config.get("family_members", {})
        n = 0
        for display_name, member in family.items():
            if member.get("role") == "friend":
                continue
            person_id = (
                member.get("canonical_id")
                or person_registry.resolve(display_name)
                or member.get("first_name", display_name)
            ).lower()
            if not person_id:
                continue
            await db_writes.routed("create_cognitive_task", 
                type="consolidation",
                payload={"person_id": person_id},
                priority=5,
            )
            n += 1
        log.info("consolidation_enqueue: queued %d task(s)", n)
    except Exception as e:
        log.error(f"consolidation_enqueue_task error: {e}", exc_info=True)
        raise


async def routine_decay_task():
    """Phase 26-02: weekly Sunday 04:00 confidence decay for stale routines."""
    if datetime.now(TZ).weekday() != 6:  # 6 = Sunday
        return
    try:
        n = await db_writes.routed("decay_routines", decay_per_run=0.05)
        log.info("routine_decay: decayed %d stale routines", n)
    except Exception as e:
        log.error(f"routine_decay_task error: {e}", exc_info=True)
        raise


# ── Phase 26-03: StudyGuideWorker triggers (10-min scanner + nightly sweep) ──

async def _scan_study_events():
    """Shared logic — find tagged events in the lookahead window and enqueue
    study_guide tasks for any without an existing queued/active/done task."""
    from cognitive_workers.study_detection import is_study_event, ensure_study_task
    cog_cfg = config.get("cognitive_workers", {}).get("study_guide", {})
    lookahead_h = int(cog_cfg.get("lookahead_hours", 24))
    # get_events_starting takes (start, days) — ceil hours to whole days
    days = max(1, (lookahead_h + 23) // 24)
    events = await cal.get_events_starting(datetime.now(TZ), days)
    if not events:
        return 0
    # Optional whitelist; if empty we accept any attendee.
    students = set(s.lower() for s in (config.get("cognitive_workers", {}).get("study_students", []) or []))
    enqueued = 0
    skipped_no_owner: list[str] = []
    for ev in events:
        if not is_study_event(ev, config):
            continue
        # Calendar events come from calendar_service with attendees populated
        # (e.g. ["Child1"]); some legacy paths use `owners` instead.
        attendees = [a for a in (ev.get("attendees") or []) + (ev.get("owners") or []) if a]
        if students:
            attendees = [a for a in attendees if a.lower() in students]
        if not attendees:
            skipped_no_owner.append((ev.get("summary") or "?")[:60])
            continue
        owner = attendees[0].lower()
        new_id = await ensure_study_task(ev, person_id=owner)
        if new_id:
            enqueued += 1
    if enqueued:
        log.info("study_scan: enqueued %d study_guide task(s)", enqueued)
    if skipped_no_owner:
        log.info("study_scan: skipped %d study event(s) with no attendee: %s",
                 len(skipped_no_owner), skipped_no_owner[:5])
    return enqueued


async def study_scan_task():
    """Phase 26-03: 10-min scan of upcoming events — on-create coverage for tagged events."""
    try:
        await _scan_study_events()
    except Exception as e:
        log.error(f"study_scan_task error: {e}", exc_info=True)
        raise


async def study_nightly_sweep_task():
    """Phase 26-03: guaranteed nightly sweep — re-runs detection (idempotent)."""
    try:
        await _scan_study_events()
    except Exception as e:
        log.error(f"study_nightly_sweep_task error: {e}", exc_info=True)
        raise


async def weekly_cognitive_report_task():
    """Phase 26-05: Sunday 09:00 — per-worker cost+runs digest to #anvil."""
    if datetime.now(TZ).weekday() != 6:  # 6 = Sunday
        return
    try:
        from eval_service import weekly_cognitive_report_worker
        await weekly_cognitive_report_worker(config, router)
    except Exception as e:
        log.error(f"weekly_cognitive_report_task error: {e}", exc_info=True)
        raise


async def dead_letter_digest_task():
    """Nightly 04:30 — post any dead_letter cognitive tasks from the last 24 h to #anvil.

    Silent if there are none. Surfaces silent failures before they compound.
    """
    try:
        rows = await get_database().get_dead_letter_tasks_since(hours=24)
        if not rows:
            return
        anvil_id = config.get("anvil_channel_id")
        if not anvil_id:
            return
        lines = ["⚠️ **Dead-letter cognitive tasks (last 24 h)**"]
        for r in rows:
            err = (r.get("error") or "")[:120]
            lines.append(
                f"• `{r['type']}` id={r['id']} retries={r['retry_count']}"
                + (f" — {err}" if err else "")
            )
        from cross_container import post_to_anvil

        await post_to_anvil("\n".join(lines), bot=bot, config=config)
        log.info("dead_letter_digest: posted %d items to #anvil", len(rows))
    except Exception as e:
        log.error("dead_letter_digest_task error: %s", e, exc_info=True)
        raise



async def personal_tasks_task():
    """Process due personal tasks/reminders and due automations."""
    MAX_PROMPTS_PER_CYCLE = 5  # prevent DM / write storms on the SSD
    try:
        now = datetime.now(TZ)
        now_iso = now.isoformat()

        prompt_cooldown_mins = int(config.get("task_prompt_cooldown_minutes", 30))
        escalate_after_snoozes = int(config.get("task_snooze_escalation_count", 3))

        due_tasks = await get_database().list_due_tasks(now_iso)
        prompts_sent = 0
        for task in due_tasks:
            if prompts_sent >= MAX_PROMPTS_PER_CYCLE:
                break

            task_id = int(task["id"])

            last_prompted_raw = task.get("last_prompted_at")
            if last_prompted_raw:
                try:
                    last_prompted = datetime.fromisoformat(last_prompted_raw)
                    if (now - last_prompted).total_seconds() < prompt_cooldown_mins * 60:
                        continue
                except Exception:
                    pass

            assignee_id = _person_to_discord_id(task.get("assigned_to", ""))
            if not assignee_id:
                continue

            due_text = task.get("due_at")
            due_line = ""
            if due_text:
                try:
                    due_dt = datetime.fromisoformat(due_text)
                    due_line = f" (due {due_dt.strftime('%a %-I:%M %p')})"
                except Exception:
                    due_line = f" (due {due_text})"

            msg = f"⏰ Task reminder: **{task.get('title','Task')}**{due_line}"
            if task.get("remind_visibility") == "channel":
                await router.notify(router.notification(
                    recipient_id=str(config.get("schedule_channel_id", 0)),
                    message=f"<@{assignee_id}> {msg}\nUse `/task_done {task_id}` when done or `/task_snooze {task_id}` to snooze.",
                ))
            else:
                try:
                    user = bot.get_user(int(assignee_id)) or await bot.fetch_user(int(assignee_id))
                    view = TaskReminderView(task_id, task.get("assigned_to", ""))
                    await user.send(msg, view=view)
                except Exception as e:
                    log.warning(f"process_due_tasks: DM with buttons failed for {assignee_id}: {e}")
                    await router.notify(router.notification(
                        recipient_id=str(assignee_id),
                        message=msg + f"\nUse `/task_done {task_id}` when done or `/task_snooze {task_id}` to snooze.",
                    ))

            await db_writes.routed("mark_task_prompted", task_id, now_iso)
            await db_writes.routed("add_task_event", task_id, "prompted", None, {"visibility": task.get("remind_visibility", "private")})
            prompts_sent += 1

            if int(task.get("snooze_count") or 0) >= escalate_after_snoozes and not task.get("escalated_at"):
                assigner_id = _person_to_discord_id(task.get("assigned_by", ""))
                if assigner_id:
                    await router.notify(router.notification(
                        recipient_id=str(assigner_id),
                        message=(
                            f"FYI: {person_display_name(task.get('assigned_to',''))} has snoozed task "
                            f"#{task_id} (**{task.get('title','Task')}**) {task.get('snooze_count')} times."
                        ),
                    ))
                await db_writes.routed("mark_task_escalated", task_id, now_iso)
                await db_writes.routed("add_task_event", task_id, "escalated", None, {"reason": "snooze_count"})

        due_automations = await get_database().list_due_automations(now_iso)
        for auto in due_automations:
            try:
                owner_person_id = auto.get("person_id", "")
                owner_discord_id = _person_to_discord_id(owner_person_id)
                if not owner_discord_id:
                    continue

                audience_scope = auto.get("audience_scope", "self")
                if audience_scope == "everyone":
                    await router.notify(router.notification(
                        recipient_id=str(config.get("schedule_channel_id", 0)),
                        message=f"🔔 {person_display_name(owner_person_id)} reminder for everyone: {auto.get('message','')}",
                    ))
                else:
                    await router.notify(router.notification(
                        recipient_id=str(owner_discord_id),
                        message=f"🔔 Reminder: {auto.get('message','')}",
                    ))

                tz_name = auto.get("timezone") or config.get("timezone", "America/Halifax")
                next_run = next_automation_run(
                    auto.get("schedule_kind", "weekly"),
                    auto.get("schedule_payload", {}),
                    tz_name,
                    after_dt=now.astimezone(ZoneInfo(tz_name)),
                )
                next_run_iso = next_run.astimezone(TZ).isoformat() if next_run else None
                await db_writes.routed("mark_automation_triggered", int(auto["id"]), now_iso, next_run_iso)

                if auto.get("schedule_kind") == "once":
                    await db_writes.routed("set_automation_active", int(auto["id"]), False)
            except Exception as auto_err:
                log.error("automation id=%s error (deactivating): %s", auto.get("id"), auto_err, exc_info=True)
                try:
                    await db_writes.routed("set_automation_active", int(auto["id"]), False)
                except Exception:
                    pass
    except Exception as e:
        log.error(f"personal_tasks_task error: {e}", exc_info=True)
        raise


async def network_monitor_task():
    """DM the configured recipient when a MAC address appears on either network for the first time."""
    try:
        import json as _json
        import pathlib as _pathlib
        from ha_service import ha_service as _ha

        STORE = _pathlib.Path("/data/network_devices.json")

        stored: dict = {}
        if STORE.exists():
            try:
                stored = _json.loads(await asyncio.to_thread(STORE.read_text))
            except Exception as e:
                log.warning(f"network_monitor: couldn't read store: {e}")

        new_devices: list[dict] = []

        # 1. Unifi active clients
        presence_cfg = config.get("presence", {})
        unifi_host   = presence_cfg.get("unifi_host", "https://192.168.1.X")  # common default; override in config.json presence.unifi_host
        ssl_verify   = bool(presence_cfg.get("unifi_ssl_verify", False))
        unifi_key    = os.environ.get("UNIFI_KEY")
        if unifi_key:
            try:
                session = get_session()
                async with session.get(
                        f"{unifi_host}/proxy/network/api/s/default/stat/sta",
                        headers={"x-api-key": unifi_key},
                        ssl=ssl_verify,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status == 200:
                            for c in (await resp.json()).get("data", []):
                                mac = (c.get("mac") or "").lower()
                                if not mac or mac in stored:
                                    continue
                                new_devices.append({
                                    "mac": mac,
                                    "vendor": str(c.get("oui") or c.get("dev_vendor") or ""),
                                    "ip": c.get("ip") or c.get("last_ip") or "",
                                    "label": c.get("name") or c.get("hostname") or "",
                                    "network": "Unifi",
                                })
            except Exception as e:
                log.error(f"network_monitor: Unifi fetch failed: {e}")

        # 2. HA network scanner
        scanner_entity = config.get("home_assistant", {}).get("network_scanner_entity", "")
        if scanner_entity:
            try:
                scanner = await _ha.get_state(scanner_entity)
                if scanner:
                    seen_macs = {d["mac"] for d in new_devices}
                    for d in scanner.get("attributes", {}).get("devices", []):
                        mac = (d.get("mac") or "").lower().replace("-", ":")
                        if not mac or mac in stored or mac in seen_macs:
                            continue
                        new_devices.append({
                            "mac": mac,
                            "vendor": str(d.get("vendor") or ""),
                            "ip": d.get("ip") or "",
                            "label": "",
                            "network": "Google WiFi",
                        })
            except Exception as e:
                log.error(f"network_monitor: HA scanner fetch failed: {e}")

        if not new_devices:
            return

        # Enrich missing vendors via OUI lookup
        async def _oui_lookup(mac: str) -> str:
            first_octet = int(mac.split(":")[0], 16)
            if first_octet & 0x02:
                return "randomized MAC"
            try:
                _s = get_session()
                async with _s.get(
                        f"https://api.maclookup.app/v2/macs/{mac}",
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            return data.get("company") or ""
            except Exception:
                pass
            return ""

        for d in new_devices:
            if not d["vendor"]:
                d["vendor"] = await _oui_lookup(d["mac"])

        # Persist all new MACs as seen so we don't re-alert (RO /data on discord → cognition)
        now_iso = datetime.now(TZ).isoformat()
        for d in new_devices:
            stored[d["mac"]] = {"first_seen": now_iso, "vendor": d["vendor"]}
        try:
            await db_writes.routed("save_network_devices_store", data=stored)
        except Exception as e:
            log.error(f"network_monitor: failed to persist seen MACs: {e}")
            return

        # Resolve DM recipient
        recipient_id = config.get("network", {}).get("alert_discord_id")
        if not recipient_id:
            person = person_registry.get(person_registry.resolve("dad"))
            if person:
                recipient_id = person.get("discord_id")

        if not recipient_id:
            log.warning("network_monitor: no alert recipient — set network.alert_discord_id in config")
            return

        try:
            user = await bot.fetch_user(int(recipient_id))
        except Exception as e:
            log.error(f"network_monitor: can't fetch Discord user {recipient_id}: {e}")
            return

        n = len(new_devices)
        lines = [f"**{'New device' if n == 1 else f'{n} new devices'} on the network**"]
        for d in new_devices:
            label = d["label"] or d["vendor"] or "unknown device"
            lines.append(f"• `{d['mac']}` — {label} · {d['network']}" + (f" · {d['ip']}" if d["ip"] else ""))
        lines.append("\nName them in the Bernie network panel.")
        await user.send("\n".join(lines))
        log.info(f"network_monitor: alerted {user.display_name} — {n} new device(s)")
    except Exception as e:
        log.error(f"network_monitor_task error: {e}", exc_info=True)
        raise


async def sqlite_backup_task():
    """family-bot-c79.5: nightly VACUUM INTO backup + retention."""
    try:
        import database as db

        keep = int(config.get("db_backup_keep_days", 14))
        path = await db.backup_db_vacuum_into(keep_days=keep)
        if path:
            log.info("sqlite_backup_task: %s", path)
    except Exception as e:
        log.error("sqlite_backup_task failed: %s", e, exc_info=True)
        raise


def register_cognition_bts_tasks(bts) -> None:
    """Thin shim — table lives in jobs.bts_registration (family-bot-8lx.3)."""
    import sys
    from jobs.bts_registration import register_cognition_bts_tasks as _reg

    _reg(bts, sys.modules[__name__])


async def db_wal_checkpoint_task():
    """40B-2A: passive WAL checkpoint (~30 min). No-op on NFS DELETE journal."""
    await get_database().wal_checkpoint_passive()


# ─────────────────────────────────────────────────────────────────────────────
# BOT EVENTS
# ─────────────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    person_registry.load(config)
    _rebuild_person_legacy(config)

    # Schema in on_ready only for monolith rollback; discord/api skip (RO mount).
    # Split cognition runs init_db via main.py _common_setup.
    if os.environ.get("ROLE", "monolith") == "monolith":
        await get_database().init_db()
        await get_database().ensure_email_schema()
        await get_database().ensure_pending_hitl_schema()
        await get_database().ensure_network_watchman_schema()

    from tools import load_all_domains
    load_all_domains()
    log.info("Tool domains loaded at startup")

    try:
        from services.live_snapshot import refresh_live_snapshot
        _snap_session = None
        try:
            _snap_session = get_session()
        except RuntimeError:
            log.debug(
                "on_ready: no HTTP session yet; weather leg skipped in snapshot warm",
            )
        await refresh_live_snapshot(
            config=config, cal_service=cal, session=_snap_session,
        )
        log.info("Live snapshot warmed at startup")
    except Exception as e:
        log.warning("Live snapshot warm at startup failed: %s", e)

    # Setup HITL (Phase 29 Wave C)
    from db_client import wait_for_cognition_writer, writes_locally
    if not writes_locally():
        if await wait_for_cognition_writer(timeout_s=90):
            log.info("on_ready: cognition writer ready")
        else:
            log.warning("on_ready: cognition writer not ready; DB RPC writes may fail until up")

    from hitl.hitl_discord import (
        init_production_refs,
        set_anvil_audit_bot,
        set_inline_notifier,
        send_hitl_approval_dms,
        register_pending_hitl_views,
    )
    init_production_refs()
    set_inline_notifier(lambda pid: send_hitl_approval_dms(pid, bot))
    set_anvil_audit_bot(bot)
    await register_pending_hitl_views(bot)
    _role = os.environ.get("ROLE", "monolith")
    if _role in ("monolith", "cognition"):
        try:
            from migrate_tasks_v32 import migrate as _migrate_tasks
            _n = await _migrate_tasks()
            log.info(f"on_ready: unified_tasks migration copied {_n} row(s)")
        except Exception as _mt_err:
            log.warning(f"on_ready: unified_tasks migration failed (non-fatal): {_mt_err}")
    from transit_discord import init_transit, register_bus_commands, transit_zones_weekly_refresh
    import sys
    from slash import register_all as register_slash_commands

    register_slash_commands(tree, sys.modules[__name__])
    register_bus_commands(tree)

    guild = discord.Object(id=config["guild_id"])
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    log.info("Slash commands synced to guild %s (includes /bus)", config["guild_id"])

    init_transit(bot)
    try:
        from transit_service import refresh_zones

        await refresh_zones(force=True)
    except Exception as e:
        log.warning("Transit zone cache init failed (non-fatal): %s", e)

    try:
        await cal.warmup()
    except Exception as e:
        log.warning(f"Calendar warmup failed (will retry on first fetch): {e}")
        
    bts = get_scheduler()
    # ── BTS owner tagging for Wave 2b container split ────────────────────────
    # owner    = "discord"   → runs only in bernie-discord (needs bot client,
    #                          slash commands, presence, reminders, etc.)
    #          = "cognition" → heavy LLM / worker output; will run in
    #                          bernie-cognition after the split.
    #                          During the 1-night shadow these still execute
    #                          inside the discord container because they post
    #                          to Discord. The minimal /internal/post hook
    #                          (see plan) will let cognition call back into
    #                          the discord container over the Docker network.
    #
    # Current cognition-owned tasks (need cross-container posting path):
    #   • nightly_eval          — 50+ judge_pair calls + posts digest to #anvil
    #   • dead_letter_digest    — surfaces failed cognitive tasks to #anvil
    #
    # tier     = immediate / can-defer / overnight-only
    # family-bot-8lx.3: registration table extracted to jobs.bts_registration
    import sys
    from jobs.bts_registration import register_discord_bts_tasks

    register_discord_bts_tasks(bts, sys.modules[__name__])

    await bts.start_all()
    
    log.info(f"✅ {bot.user} is live.")

    # Wave 2b Docker healthcheck marker — written when the Discord client
    # and all background tasks (including internal post server) are ready.
    try:
        open("/tmp/bernie_discord_ready", "w").close()
    except Exception:
        pass
    channel = get_schedule_channel()
    if channel:
        startup_key = f"startup_{datetime.now(TZ).strftime('%Y-%m-%d')}"
        if not await get_database().is_reminder_sent(startup_key, 0):
            await db_writes.mark_reminder_sent(startup_key, 0)
            await db_writes.add_message(config["schedule_channel_id"], "assistant", "Bernie is online.")
            await router.notify(router.notification(
                recipient_id=str(config["schedule_channel_id"]),
                message="👋 Bernie is online! Just chat with me naturally — try *\"what's on today?\"* or *\"add soccer practice tomorrow at 3pm\"*"
            ))


@bot.event
async def on_close():
    log.info("Bot on_close called.")


# --- Global state for rolling session windows ---
_last_act: dict[int, float] = {}
_session_ids: dict[int, str] = {}
_session_start: dict[int, float] = {}  # timestamp of the first message in each session window


async def _send_chunked(channel, text: str, *, is_dm: bool = False, files=None):
    """Send long replies in Discord-safe chunks."""
    from discord_chunk import send_chunked

    return await send_chunked(channel, text, is_dm=is_dm, files=files)


async def _handle_message(message: discord.Message, chat_fn, **kwargs):
    is_dm = kwargs.get('is_dm', False)
    log.info(f"_handle_message: is_dm={is_dm}, kwargs={list(kwargs.keys())}")
    from discord_typing import typing_ack, typing_heartbeat

    await typing_ack(message.channel)
    async with typing_heartbeat(message.channel):
        try:
            # Compute session window before fetching history so we can scope the
            # history query to the current session. A 30-min idle gap rolls a new
            # session; _session_start tracks when the current window began.
            now_ts = _time_mod.time()
            last_ts = _last_act.get(message.channel.id, 0)
            if now_ts - last_ts > 1800:
                _session_ids[message.channel.id] = f"{message.channel.id}-{int(now_ts * 1000)}"
                _session_start[message.channel.id] = now_ts
            _last_act[message.channel.id] = now_ts
            session_id = _session_ids[message.channel.id]
            conversation_id = session_id  # Forward as conversation_id for DB compatibility

            # Perf instrumentation: start before history/session setup so pre-chat
            # work is included in total_ms; setup mark fires right before chat_fn.
            turn_id = f"{session_id}:{int(_time_mod.time()*1000)}"
            async with TurnTimer(
                turn_id=turn_id,
                channel_id=str(message.channel.id),
                person_id=None,  # resolved below; timer only needs stable turn_id early
                session_id=session_id,
            ) as _tt:
                history = await get_database().get_history(message.channel.id, since=_session_start.get(message.channel.id))
                if message.content and message.content.strip():
                    asyncio.create_task(db_writes.add_message(message.channel.id, "user", message.content))

                # Resolve family member ID from Discord ID using the centralized registry
                person_id = person_registry.resolve(message.author.id)
                person = person_registry.get(person_id) if person_id else None
                _tt.person_id = person_id

                group = kwargs.get("group")
                if person:
                    if not group:
                        group = person.get("role", "kids")

                # Pop potentially conflicting arguments from kwargs, then re-pass
                # explicit values (is_dm must reach chat_general for DM prompt rules).
                kwargs.pop("group", None)
                kwargs.pop("person_name", None)
                kwargs.pop("actor_id", None)
                kwargs.pop("is_dm", None)
                kwargs.pop("session_id", None)

                _tt.mark("setup")
                response = await chat_fn(
                    user_message=message.content,
                    history=history,
                    person_name=person_registry.display_name(person_id) if person_id else message.author.display_name,
                    actor_id=person_id,
                    group=group,
                    session_id=session_id,
                    conversation_id=conversation_id,
                    channel_id=str(message.channel.id),
                    is_dm=is_dm,
                    **kwargs
                )
                if "llm" not in _tt.phases:
                    _tt.mark("llm")
                response = _strip_model_artifacts(response)
                asyncio.create_task(db_writes.add_message(message.channel.id, "assistant", response))

                # Intercept camera snapshot markdown links (still inside turn)
                import re
                import io
                from frigate_service import frigate_service

                camera_pattern = r"!\[.*?\]\(\s*/api/cameras/([\w-]+)/snapshot\s*\)"
                camera_matches = re.findall(camera_pattern, response, re.IGNORECASE)

                files = []
                if camera_matches:
                    for cam_id in camera_matches:
                        log.info(f"Detected camera snapshot request for {cam_id} in response")
                        result = await frigate_service.get_snapshot(cam_id)
                        if result:
                            data, content_type = result
                            filename = f"{cam_id}_snapshot.jpg"
                            files.append(discord.File(io.BytesIO(data), filename=filename))
                        else:
                            log.error(f"Failed to fetch snapshot for camera: {cam_id}")
                            if "**(Error: Could not fetch snapshot for " not in response:
                                response += f"\n\n*(Error: Could not fetch snapshot for {cam_id})*"
                    response = re.sub(camera_pattern, "", response, flags=re.IGNORECASE).strip()

                if files:
                    sent_msg = await _send_chunked(
                        message.channel, response, is_dm=is_dm, files=files,
                    )
                else:
                    sent_msg = await _send_chunked(message.channel, response, is_dm=is_dm)
                _tt.mark("send")

            # Drain any research-delivery choices Bernie queued during this turn. (post-turn)
            # For each pending research task tied to this Discord user, react
            # 💬 (DM) and ✉️ (email) on the just-sent reply and persist a
            # mapping row so on_reaction_add can update the task's payload.
            try:
                import research_delivery_queue as _rdq
                pending = _rdq.drain(str(message.author.id))
                if pending and sent_msg is not None:
                    for entry in pending:
                        await sent_msg.add_reaction("💬")
                        await sent_msg.add_reaction("✉️")
                        await db_writes.store_message_mapping(
                            sent_msg.id,
                            f"research_task:{entry['task_id']}",
                            entry.get("topic", "")[:200],
                            message_type="research_choice",
                        )
            except Exception as e:
                log.warning("research-delivery choice prompt failed: %s", e)

            # Post any pending calendar drafts Claude created during this turn
            for draft in await get_database().get_unposted_drafts():
                draft_id = draft["draft_id"]
                embed = build_draft_embed(draft)
                draft_msg = await message.channel.send("Confirm this event?", embed=embed)
                for emoji in ["✅", "❌"]:
                    await draft_msg.add_reaction(emoji)
                await db_writes.store_message_mapping(draft_msg.id, draft_id, draft["summary"])
                await db_writes.routed("mark_draft_posted", draft_id)
        except Exception as e:
            ch_name = getattr(message.channel, 'name', 'DM')
            log.error(f"Error in {ch_name}: {e}", exc_info=True)
            await message.channel.send("Sorry, something went wrong. Try again in a moment!")


async def process_image_via_ollama(message: discord.Message):
    """
    Directly routes an image to Ollama on deba.
    """
    if not message.attachments:
        return None

    attachment = message.attachments[0]
    
    # Safety: Limit to 10MB to prevent OOM during base64 encoding
    if attachment.size > 10 * 1024 * 1024:
        return "⚠️ Image is too large (max 10MB for vision analysis)."

    valid_extensions = ['png', 'jpg', 'jpeg', 'webp']
    if not any(attachment.filename.lower().endswith(ext) for ext in valid_extensions):
        return None

    # Use config-driven URL
    ollama_url = config.get("llm_fallback", {}).get("url", "http://192.168.1.X:11434")  # placeholder; set llm_fallback.url or ollama_base_url in config.json
    endpoint = f"{ollama_url.rstrip('/')}/api/chat"

    # Use existing aiohttp session
    session = get_session()
    
    # 1. Get image data
    try:
        async with session.get(attachment.url) as resp:
            if resp.status != 200:
                return "⚠️ Failed to download image from Discord."
            img_data = await resp.read()
            raw_base64_image = await asyncio.get_running_loop().run_in_executor(
                None, lambda: base64.b64encode(img_data).decode('utf-8')
            )
    except Exception as e:
        return f"⚠️ **Network Error:** Could not fetch image. {e}"

    # 2. Construct Ollama payload — vision model name is config-driven (no hardcoded identifiers)
    vision_model = config.get("vision_model") or "qwen3-vl:8b"
    payload = {
        "model": vision_model,
        "messages": [
            {
                "role": "user", 
                "content": message.content if message.content else "What is in this image? Be detailed.",
                "images": [raw_base64_image]
            }
        ],
        "stream": False
    }

    # 3. Request analysis
    try:
        async with session.post(endpoint, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                return f"⚠️ **Homelab Error:** Ollama returned status {resp.status}. {err_text}"

            data = await resp.json()
            content = data['message']['content']
            try:
                from langfuse_logger import log_generation
                fire_and_forget(log_generation(
                    model=vision_model,
                    user_input=message.content or "[image]",
                    output=content,
                    input_tokens=data.get("prompt_eval_count", 0) or 0,
                    output_tokens=data.get("eval_count", 0) or 0,
                    name="vision",
                    actor_id=str(message.author.id),
                    triggered_by="discord",
                    metadata={"attachment": message.attachments[0].filename},
                    cost_usd=0.0,
                ))
            except Exception:
                log.debug("langfuse vision trace failed (non-fatal)", exc_info=True)
            return content
    except Exception as e:
        return f"⚠️ **Homelab Error:** Could not reach the Ollama worker. {e}"


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    is_dm = message.guild is None
    log.info(f"on_message: author={message.author} is_dm={is_dm} channel={getattr(message.channel, 'id', '?')}")
    
    is_schedule = message.channel.id == config.get("schedule_channel_id", 0)
    is_furnace = message.channel.id == config.get("furnace_channel_id", 0)
    is_slag = message.channel.id == config.get("slag_channel_id", 0)
    is_anvil = message.channel.id == config.get("anvil_channel_id", 0)
    is_security = message.channel.id == config.get("frigate", {}).get("notification_channel_id", 0) or message.channel.id == config.get("security_channel_id", 0)

    # --- Vision Handler (Option B) ---
    if message.attachments:
        if is_dm or is_schedule or is_slag or is_anvil or is_security:
            from discord_typing import typing_ack, typing_heartbeat
            await typing_ack(message.channel)
            async with typing_heartbeat(message.channel):
                response_text = await process_image_via_ollama(message)
                if response_text:
                    # 1. Log to DB for memory persistence (ONLY if successful, no ⚠️ errors)
                    if not response_text.startswith("⚠️"):
                        user_note = f"[Image Attached: {message.attachments[0].filename}] {message.content}"
                        await db_writes.add_message(message.channel.id, "user", user_note)
                        await db_writes.add_message(message.channel.id, "assistant", response_text)
                    
                    # 2. Send to Discord (chunked for 2000 chars)
                    await _send_chunked(message.channel, response_text, is_dm=is_dm)
                    return
        elif is_furnace:
            await message.channel.send("🚫 Image processing is only available in DMs, #smithy, #slag, and #anvil.")
            return
    # ---------------------------------

    if is_dm:
        await _handle_message(message, claude_chat, config=config, is_dm=True)
    elif is_anvil:
        # Resolve member from cache (handles discord.User after reconnect); trust Discord perms on cache miss
        member = message.guild.get_member(message.author.id) if message.guild else None
        grp = get_person_group(member) if member else "admin"
        if grp != "admin":
            return
        await _handle_message(message, claude_chat, config=config, group="admin")
    elif is_schedule:
        mode = _channel_mode(message.channel.id)
        if mode in ("listen", "mention_only") and not _bot_mentioned(message):
            await bot.process_commands(message)
            return
        group = get_person_group(message.author)
        await _maybe_coalesce(message, claude_chat, config=config, group=group)
    elif is_furnace:
        mode = _channel_mode(message.channel.id)
        if mode in ("listen", "mention_only") and not _bot_mentioned(message):
            await bot.process_commands(message)
            return
        group = get_person_group(message.author)
        await _maybe_coalesce(message, claude_chat_general, config=config, group=group)
    elif is_slag:
        mode = _channel_mode(message.channel.id)
        if mode in ("listen", "mention_only") and not _bot_mentioned(message):
            await bot.process_commands(message)
            return
        group = get_person_group(message.author)
        await _maybe_coalesce(message, claude_chat_general, config=config, group=group)
    elif is_security:
        mode = _channel_mode(message.channel.id)
        if mode in ("listen", "mention_only") and not _bot_mentioned(message):
            await bot.process_commands(message)
            return
        group = get_person_group(message.author)
        # Using claude_chat_general which supports modes, it will resolve to 'security' mode
        await _maybe_coalesce(message, claude_chat_general, config=config, group=group)
    else:
        await bot.process_commands(message)
        return

    await bot.process_commands(message)


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.Member):
    if user.bot:
        return
    emoji = str(reaction.emoji)
    if emoji not in ["✅", "❌", "🤔", "💬", "✉️"]:
        return
    mapping = await get_database().get_mapping_for_message(reaction.message.id)
    if not mapping:
        return

    message_type = mapping.get("message_type", "event")
    event_id = mapping["event_id"]

    # Proactive nudge acknowledgement / dismissal
    if message_type == "proactive_nudge":
        if emoji not in ("✅", "❌"):
            return
        parts = event_id.split(":", 2)
        person_id = parts[1] if len(parts) > 1 else None
        nudge_prefix = parts[2] if len(parts) > 2 else ""
        action = "ack" if emoji == "✅" else "dismiss"
        event_type = f"proactive_nudge_{action}"
        try:
            await db_writes.routed("log_activity", 
                event_type=event_type,
                description=nudge_prefix,
                person_id=person_id,
            )
            if emoji == "✅":
                await reaction.message.reply("✅ Got it! Acknowledged.")
            else:
                try:
                    await reaction.message.delete()
                except Exception:
                    await reaction.message.reply("❌ Dismissed.")
        except Exception as e:
            log.warning("proactive_nudge reaction handling failed: %s", e)
        return

    # Kid-initiated email approval (#smithy).
    if message_type == "email_pending":
        if emoji not in ("✅", "❌"):
            return
        if not event_id.startswith("email_pending:"):
            return
        try:
            pending_id = int(event_id.split(":", 1)[1])
        except Exception:
            return
        approver_group = get_person_group(reaction.user)
        if approver_group not in ("admin", "parents"):
            return
        try:
            pending = await get_database().get_email_pending(pending_id)
            if not pending or pending.get("status") != "pending":
                return
            if emoji == "❌":
                await db_writes.routed("resolve_email_pending", 
                    pending_id, status="denied", decided_by=str(reaction.user.id)
                )
                await db_writes.routed("log_activity", 
                    event_type="email_pending_denied",
                    description=f"Denied send to {pending.get('recipient')}",
                    person_id=str(reaction.user.id),
                )
                await reaction.message.reply("❌ Email cancelled.")
                return
            import json as _json
            from email_service import EmailPolicyError, EmailRateLimitError, send

            claimed = await db_writes.routed("claim_email_pending_for_send", 
                pending_id, decided_by=str(reaction.user.id)
            )
            if not claimed:
                return

            cc_raw = pending.get("cc") or "[]"
            cc = _json.loads(cc_raw) if isinstance(cc_raw, str) else (cc_raw or [])
            try:
                msg_id = await send(
                    pending["recipient"],
                    pending["subject"],
                    pending["body"],
                    cc=cc or None,
                    requester_id=pending.get("requester_id") or "unknown",
                    requester_role="parents",
                    config=config,
                    reply_to_gmail_id=pending.get("reply_to_gmail_id"),
                    thread_id=pending.get("thread_id"),
                    policy_checked=True,
                )
                await db_writes.routed("finalize_email_pending", 
                    pending_id, status="sent", decided_by=str(reaction.user.id)
                )
                await db_writes.routed("log_activity", 
                    event_type="email_pending_approved",
                    description=f"Approved send to {pending.get('recipient')} (id {msg_id})",
                    person_id=approver_group,
                )
                try:
                    await reaction.message.edit(
                        content=(reaction.message.content or "")
                        + f"\n\n✅ **Sent** by {reaction.user.display_name}."
                    )
                    await reaction.message.clear_reactions()
                except Exception:
                    await reaction.message.reply(f"✅ Sent. (Gmail id: `{msg_id}`)")
            except (EmailPolicyError, EmailRateLimitError) as e:
                await db_writes.routed("finalize_email_pending", 
                    pending_id, status="pending", decided_by=None
                )
                await reaction.message.reply(f"❌ Could not send: {e}")
                return
            except Exception:
                await db_writes.routed("finalize_email_pending", 
                    pending_id, status="pending", decided_by=None
                )
                raise
        except Exception as e:
            log.warning("email_pending reaction handling failed: %s", e)
        return

    # Research delivery choice — flip the pending research task between DM/email.
    if message_type == "research_choice":
        if emoji not in ("💬", "✉️"):
            return
        if not event_id.startswith("research_task:"):
            return
        try:
            task_id = int(event_id.split(":", 1)[1])
        except Exception:
            return
        new_delivery = "dm" if emoji == "💬" else "email"
        try:
            updated = await db_writes.routed("update_cognitive_task_payload", task_id, {"delivery": new_delivery})
            if updated:
                label = "DM" if new_delivery == "dm" else "email"
                await reaction.message.reply(f"Got it — I'll send the research as {label}.")
        except Exception as e:
            log.warning("research_choice reaction handling failed: %s", e)
        return

    if message_type == "task_approval":
        if str(reaction.emoji) not in ["✅", "❌"]:
            return
        if not event_id.startswith("task:"):
            return
        try:
            task_id = int(event_id.split(":", 1)[1])
        except Exception:
            return

        svc = _unified_tasks()
        if not svc:
            return

        task = await svc.task_store.get_task(task_id)
        if not task or task.get("status") != "done":
            return

        actor_person_id = _discord_to_person_id(user.id) or str(user.id)
        approver_person_id = task.get("approver_person_id") or task.get("assigned_by")
        if actor_person_id != approver_person_id and get_person_group(user) != "admin":
            return

        approved = str(reaction.emoji) == "✅"
        from services.unified_task_service import TaskValidationError
        try:
            await svc.approve_task(task_id, actor_id=actor_person_id, approved=approved)
        except TaskValidationError:
            return

        await _broadcast_task_update("approved" if approved else "reopened", task_id)

        if approved:
            await reaction.message.reply(f"Approved task #{task_id}.")
        else:
            await reaction.message.reply(f"Reopened task #{task_id}.")
        return

    if event_id.startswith("draft_"):
        draft = await get_database().get_draft(event_id)
        if not draft:
            return
        if str(reaction.emoji) == "✅":
            start = draft["start"]
            end = draft["end"]
            try:
                event = await cal.create_event(
                    summary=draft["summary"],
                    start=start,
                    end=end,
                    attendees=draft.get("attendees", []),
                    location=draft.get("location", ""),
                    description=draft.get("description", ""),
                    remind_minutes=draft.get("remind_minutes"),
                )
                await reaction.message.reply(
                    f"✅ Added **{event['summary']}** on {start.strftime('%A %B %-d at %-I:%M %p')}."
                )
            except Exception as e:
                await reaction.message.reply(f"❌ Couldn't add to calendar: {e}")
        elif str(reaction.emoji) == "❌":
            await reaction.message.reply("Cancelled.")
        await db_writes.routed("delete_draft", event_id)
        return

    event_title = mapping.get("event_title") or "Unknown Event"
    status_map = {"✅": "yes", "❌": "no", "🤔": "maybe"}
    if emoji not in status_map:
        return
    status = status_map[emoji]
    await db_writes.save_rsvp(event_id, user.id, user.display_name, status)
    
    if status == "yes":
        person_id = person_registry.resolve(user.id) or str(user.id)
        await record_acknowledged(person_id, event_title)
        
    log.info(f"RSVP: {user.display_name} → {status}")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if bot.user and payload.user_id == bot.user.id:
        return
    if payload.guild_id is not None:
        return
    from eval_service import handle_hitl_reaction
    asyncio.create_task(handle_hitl_reaction(
        message_id=payload.message_id,
        emoji=str(payload.emoji),
        actor_id=str(payload.user_id),
        config=config,
    ))


# ─────────────────────────────────────────────────────────────────────────────

# ── Slash helpers (used by message handlers + slash via bot module) ──
def _is_anvil(interaction: discord.Interaction) -> bool:
    return interaction.channel_id == config.get("anvil_channel_id")


def get_person_group(member: discord.Member | discord.User | None) -> str | None:
    """Return 'admin', 'parents', 'kids', or None based on Discord roles or registry."""
    if member is None:
        return None
        
    # 1. Try Discord roles (authoritative if present)
    if hasattr(member, "roles"):
        role_map = config.get("discord_roles", {})
        member_role_ids = {str(r.id) for r in member.roles}
        for group in ("admin", "parents", "kids"):
            for role_id_str, group_name in role_map.items():
                if group_name == group and role_id_str in member_role_ids:
                    return group
    
    # 2. Fall back to person registry (handles DMs and cases where roles aren't cached)
    person_id = person_registry.resolve(member.id)
    person = person_registry.get(person_id) if person_id else None
    if person:
        return person.get("role")
        
    return None


def _is_admin_group(interaction: discord.Interaction) -> bool:
    """Returns True if the interaction user has the admin role group."""
    return get_person_group(interaction.user) == "admin"


async def _send_ephemeral(interaction: discord.Interaction, content: str | None = None, embed: discord.Embed | None = None):
    if interaction.response.is_done():
        return await interaction.followup.send(content=content, embed=embed, ephemeral=True)
    return await interaction.response.send_message(content=content, embed=embed, ephemeral=True)

# Slash commands: registered from bot/slash via slash.register_all(tree, bot_module)
# (peeled family-bot-8lx.3). Bodies no longer live in this file.

if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"], log_handler=None)
