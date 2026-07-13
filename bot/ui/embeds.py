"""
bot/ui/embeds.py — Discord embed layout functions (Phase 4.2).

Pure functions: accept data, return discord.Embed. No side effects.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import discord

from config import config
from recommendation_engine import get_recommendations
from school_calendar import (
    exclude_school_from_schedule,
    school_calendar_ids,
    show_school_in_daily_summary,
)
from summary_builder import build_highlights, format_highlights
from weather_service import weather_line


def _partition_school(events: list[dict], tz) -> tuple[list[dict], list[dict]]:
    school_cals = school_calendar_ids(config)
    school = [e for e in events if e.get("calendar_id") in school_cals]
    family = [e for e in events if e.get("calendar_id") not in school_cals]
    return school, family


def build_reminder_embed(event: dict, mins_until: int) -> discord.Embed:
    embed = discord.Embed(title=f"📅 {event['summary']}", color=discord.Color.orange())
    if mins_until <= 1:
        embed.description = "⏰ **Starting NOW!**"
    else:
        embed.description = f"⏰ Starting in **{mins_until} minutes**"

    if event.get("all_day"):
        embed.add_field(name="📆 Date", value=event["start"].strftime("%A, %B %d"), inline=True)
    else:
        embed.add_field(
            name="🕐 Time",
            value=f"{event['start'].strftime('%I:%M %p %Z')} – {event['end'].strftime('%I:%M %p %Z')}",
            inline=True
        )
    if event.get("location"):
        embed.add_field(name="📍 Location", value=event["location"], inline=True)
    if event.get("attendees"):
        embed.add_field(name="👨‍👩‍👧 Who", value=", ".join(event["attendees"]), inline=False)

    embed.set_footer(text="React ✅ Going  ❌ Not going  🤔 Maybe")
    return embed


def build_summary_embed(
    events: list[dict],
    weather: dict | None = None,
    garbage: dict | None = None,
    prefix: str | None = None,
    *,
    tz,
) -> discord.Embed:
    today_dt = datetime.now(tz)
    today_str = today_dt.strftime("%A, %B %d")
    embed = discord.Embed(title=f"🌅 {today_str}", color=discord.Color.gold())

    # 1. Highlights
    rec = get_recommendations(weather) if weather else None
    show_school = show_school_in_daily_summary(config)
    schedule_events = exclude_school_from_schedule(events, config)
    summary_school_cals = school_calendar_ids(config) if show_school else set()

    highlights = build_highlights(
        schedule_events, rec, garbage is not None, tz,
        school_cals=summary_school_cals,
    )
    if highlights:
        embed.add_field(name="🔥 Today's Highlights", value=format_highlights(highlights), inline=False)

    # 2. Weather & Context (with optional ReflectionWorker prefix at the top)
    context_lines = []
    if prefix:
        context_lines.append(f"💭 _{prefix.strip()}_")
    if rec:
        context_lines.append(f"🌤 {rec.summary}")
        if rec.timing_alerts:
            context_lines.append(f"⏱ {rec.timing_alerts[0]}")
    elif weather:
        context_lines.append(f"🌤 {weather_line(weather)}")

    if garbage:
        context_lines.append(f"{garbage['icon']} **Garbage tomorrow:** {garbage['summary'].title()}")

    if context_lines:
        embed.description = "\n".join(context_lines)

    # 3. Schedule & Homework
    school, family = _partition_school(schedule_events, tz)
    if not show_school:
        school = []

    school_timed = sorted([e for e in school if not e["all_day"]], key=lambda e: e["start"])
    first_class = [school_timed[0]] if school_timed else []

    display = sorted(family + first_class, key=lambda e: e["start"])
    if not display:
        embed.add_field(name="📅 Today's Schedule", value="_Nothing on the books today! 🎉_", inline=False)
    else:
        sched_lines = []
        for ev in display:
            time_str = "All day" if ev.get("all_day") else ev["start"].strftime("%I:%M %p")
            who = f"({', '.join(ev.get('attendees', []))})" if ev.get("attendees") else ""
            line = f"• **{time_str}** — {ev['summary']} {who}"
            if ev.get("location"):
                line += f" 📍 {ev['location']}"
            sched_lines.append(line.strip())
        embed.add_field(name="📅 Today's Schedule", value="\n".join(sched_lines), inline=False)

    # Homework Category
    target_date = today_dt.date()

    def _due_date(ev):
        return ev.get("due_date", ev["end"] - timedelta(days=1)).date()

    homework_due = [e for e in school if e.get("all_day") and _due_date(e) == target_date]
    if homework_due:
        hw_lines = []
        for ev in homework_due:
            val = ev.get("description", "").strip()
            if val:
                hw_lines.append(f"• **{ev['summary']}** — {val}")
            else:
                hw_lines.append(f"• **{ev['summary']}**")
        embed.add_field(name="📚 Homework (Due Today)", value="\n".join(hw_lines), inline=False)

    embed.set_footer(text="Chat with Bernie anytime to manage events")
    return embed


def build_school_embed(events: list[dict], *, tz) -> discord.Embed:
    today = datetime.now(tz).strftime("%A, %B %d")
    embed = discord.Embed(title=f"🏫 School Schedule — {today}", color=discord.Color.green())
    if not events:
        embed.description = "No classes today!"
        return embed
    for ev in events:
        time_str = f"{ev['start'].strftime('%I:%M %p')} – {ev['end'].strftime('%I:%M %p')}"
        embed.add_field(name=ev["summary"], value=time_str, inline=False)
    return embed


def build_homework_embed(events: list[dict], *, tz) -> discord.Embed:
    today = datetime.now(tz).strftime("%A, %B %d")
    embed = discord.Embed(title=f"📚 Due Today — {today}", color=discord.Color.orange())
    if not events:
        embed.description = "Nothing due today!"
        return embed
    for ev in events:
        due_d = ev.get("due_date", ev["end"] - timedelta(days=1)).date()
        start_d = ev["start"].date()
        value = ev.get("description", "").strip() or "—"
        if due_d != start_d:
            value = f"Assigned {start_d.strftime('%b %d')} · " + value
        embed.add_field(name=ev["summary"], value=value, inline=False)
    return embed


def build_draft_embed(draft: dict) -> discord.Embed:
    start = draft["start"]
    end = draft["end"]
    embed = discord.Embed(
        title=f"📅 {draft['summary']}",
        color=discord.Color.orange()
    )
    embed.add_field(name="When", value=start.strftime("%A %B %-d · %-I:%M – ") + end.strftime("%-I:%M %p %Z"), inline=False)
    if draft.get("location"):
        embed.add_field(name="Where", value=draft["location"], inline=True)
    if draft.get("attendees"):
        embed.add_field(name="Attendees", value=", ".join(draft["attendees"]), inline=True)
    embed.set_footer(text="React ✅ to confirm · ❌ to cancel")
    return embed


def build_weekly_embed(events: list[dict], start: datetime, *, tz) -> discord.Embed:
    events = exclude_school_from_schedule(events, config)
    school_cals = school_calendar_ids(config)
    keep_pattern = re.compile(
        config.get("cognitive_workers", {}).get(
            "study_keywords",
            "test|exam|quiz|rehearsal|recital|audition|midterm|finals?|final exam",
        ),
        re.IGNORECASE,
    )

    def _keep(ev: dict) -> bool:
        if ev.get("calendar_id") not in school_cals:
            return True
        if ev.get("all_day"):
            return True
        return bool(keep_pattern.search(ev.get("summary", "")))

    events = [ev for ev in events if _keep(ev)]

    week_start = start.strftime("%B %d")
    week_end = (start + timedelta(days=6)).strftime("%B %d")
    embed = discord.Embed(
        title=f"📅 Week Ahead — {week_start} to {week_end}",
        color=discord.Color.blue()
    )
    if not events:
        embed.description = "Nothing on the books this week! 🎉"
        return embed

    by_day: dict[str, list] = defaultdict(list)
    for ev in events:
        by_day[ev["start"].strftime("%A, %B %d")].append(ev)

    for day, day_events in by_day.items():
        lines = []
        for ev in day_events:
            time_str = "All day" if ev.get("all_day") else (
                f"{ev['start'].strftime('%I:%M %p')} – {ev['end'].strftime('%I:%M %p')}"
            )
            who = ", ".join(ev.get("attendees", [])) or "Everyone"
            line = f"▸ **{ev['summary']}** — {time_str} | {who}"
            if ev.get("location"):
                line += f"\n  📍 {ev['location']}"
            lines.append(line)
        embed.add_field(name=day, value="\n".join(lines), inline=False)

    embed.set_footer(text="Have a great week!")
    return embed
