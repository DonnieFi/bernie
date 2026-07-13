"""Slash commands: school (family-bot-1od — real module, no exec)."""
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands


def register(tree: app_commands.CommandTree, m: Any) -> None:
    """Register school slash commands. *m* is the bot module (helpers + config)."""
    # Explicit bot bindings (replaces exec + _LiveNS proxy)
    TZ = getattr(m, 'TZ', None)
    _partition_school = m._partition_school
    build_homework_embed = m.build_homework_embed
    build_school_embed = m.build_school_embed
    cal = getattr(m, 'cal', None)
    import asyncio
    import os
    import re
    from collections import defaultdict
    from datetime import datetime, timedelta, time, timezone
    from zoneinfo import ZoneInfo
    try:
        import aiohttp
    except ImportError:  # pragma: no cover
        aiohttp = None  # type: ignore

    @tree.command(name="school", description="Today's full class schedule")
    async def cmd_school(interaction: discord.Interaction):
        await interaction.response.defer()
        school, _ = _partition_school(await cal.get_todays_events())
        timed = sorted([e for e in school if not e["all_day"]], key=lambda e: e["start"])
        await interaction.followup.send(embed=build_school_embed(timed))


    @tree.command(name="homework", description="Assignments and due dates")
    @app_commands.describe(timeframe="today (default), tomorrow, or week")
    @app_commands.choices(timeframe=[
        app_commands.Choice(name="today",    value="today"),
        app_commands.Choice(name="tomorrow", value="tomorrow"),
        app_commands.Choice(name="week",     value="week"),
    ])
    async def cmd_homework(interaction: discord.Interaction, timeframe: str = "today"):
        await interaction.response.defer()

        def _due_date(ev):
            return ev.get("due_date", ev["end"] - timedelta(days=1))

        if timeframe == "tomorrow":
            events = await cal.get_tomorrows_events()
            school, _ = _partition_school(events)
            target = (datetime.now(TZ) + timedelta(days=1)).date()
            due = [e for e in school if e["all_day"] and _due_date(e).date() == target]
            embed = build_homework_embed(due)
            tomorrow_dt = datetime.now(TZ) + timedelta(days=1)
            embed.title = f"📚 Due Tomorrow — {tomorrow_dt.strftime('%A, %B %d')}"
            await interaction.followup.send(embed=embed)

        elif timeframe == "week":
            events = await cal.get_week_events_from_monday()
            school, _ = _partition_school(events)
            due = [e for e in school if e["all_day"]]
            # Group by due_date (end - 1 day), not start, so multi-day assignments sort correctly
            by_day: dict[str, list] = defaultdict(list)
            for ev in due:
                due_d = ev.get("due_date", ev["end"] - timedelta(days=1))
                by_day[due_d.strftime("%A, %B %d")].append(ev)
            embed = discord.Embed(
                title=f"📚 Homework This Week",
                color=discord.Color.orange()
            )
            if not by_day:
                embed.description = "Nothing due this week!"
            else:
                for day, day_due in sorted(by_day.items(), key=lambda x: x[0]):
                    lines = [f"• {ev['summary']}" for ev in day_due]
                    embed.add_field(name=day, value="\n".join(lines), inline=False)
            await interaction.followup.send(embed=embed)

        else:
            school, _ = _partition_school(await cal.get_todays_events())
            target = datetime.now(TZ).date()
            due = [e for e in school if e["all_day"] and _due_date(e).date() == target]
            await interaction.followup.send(embed=build_homework_embed(due))

