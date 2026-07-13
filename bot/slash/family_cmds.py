"""Slash commands: family (family-bot-1od — real module, no exec)."""
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands


def register(tree: app_commands.CommandTree, m: Any) -> None:
    """Register family slash commands. *m* is the bot module (helpers + config)."""
    # Explicit bot bindings (replaces exec + _LiveNS proxy)
    TZ = getattr(m, 'TZ', None)
    build_summary_embed = m.build_summary_embed
    build_weekly_embed = m.build_weekly_embed
    cal = getattr(m, 'cal', None)
    config = m.config
    get_database = m.get_database
    get_next_collections = m.get_next_collections
    get_person_group = m.get_person_group
    get_session = m.get_session
    get_tomorrow_collection = m.get_tomorrow_collection
    get_weather = m.get_weather
    get_weather_for_request = m.get_weather_for_request
    log = m.log
    update_config = m.update_config
    weather_forecast_line = m.weather_forecast_line
    weather_line = m.weather_line
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

    @tree.command(name="weather", description="Weather forecast")
    @app_commands.describe(city="Optional city name", period="now/today/tomorrow/week/specific", date="YYYY-MM-DD when period is specific")
    @app_commands.choices(period=[
        app_commands.Choice(name="now",   value="now"),
        app_commands.Choice(name="today", value="today"),
        app_commands.Choice(name="tomorrow", value="tomorrow"),
        app_commands.Choice(name="week",  value="week"),
        app_commands.Choice(name="specific", value="specific"),
    ])
    async def cmd_weather(interaction: discord.Interaction, city: str | None = None, period: str = "now", date: str | None = None):
        await interaction.response.defer()
        report = await get_weather_for_request(city, period, get_session(), date_str=date)
        if not report:
            await interaction.followup.send("❌ Weather data unavailable right now.")
            return

        if report.get("kind") == "error":
            await interaction.followup.send(f"❌ {report.get('message', 'Weather data unavailable right now.')}")
            return

        if report.get("kind") == "week":
            embed = discord.Embed(title=f"🌤 {report['location']['display_name']} — 7-Day Forecast", color=discord.Color.blue())
            for d in report.get("days", []):
                label = f"{d['label']}, {d['date']}"
                value = weather_forecast_line(d)
                embed.add_field(name=label, value=value, inline=False)
            await interaction.followup.send(embed=embed)
            return

        embed = discord.Embed(title=f"🌤 {report['location']['display_name']}", color=discord.Color.blue())
        # period=now → kind "current" uses key "weather"; today → kind "day" uses "current"
        now_snapshot = report.get("current") or report.get("weather")
        if now_snapshot:
            embed.add_field(name="Right now", value=weather_line(now_snapshot), inline=False)
        if report.get("day"):
            d = report["day"]
            embed.add_field(name=d.get("label", d.get("date", "Forecast")), value=weather_forecast_line(d), inline=False)
        await interaction.followup.send(embed=embed)


    @tree.command(name="garbage", description="Show upcoming garbage/recycling collection")
    async def cmd_garbage(interaction: discord.Interaction):
        await interaction.response.defer()
        url = config.get("recollect_ics_url")
        if not url:
            await interaction.followup.send("❌ Garbage collection URL not configured.")
            return

        events = await get_next_collections(url, TZ, get_session(), days=14)
        if not events:
            await interaction.followup.send("📅 No upcoming collections found.")
            return

        embed = discord.Embed(title="🚛 Halifax Waste Collection", color=discord.Color.dark_grey())
        for e in events:
            date_str = e["date"].strftime("%A, %b %d")
            embed.add_field(name=f"{e['icon']} {date_str}", value=e["summary"].title(), inline=False)

        await interaction.followup.send(embed=embed)


    @tree.command(name="summary", description="Post today's schedule right now")
    async def cmd_summary(interaction: discord.Interaction):
        await interaction.response.defer()
        events = await cal.get_todays_events()
        weather = await get_weather(config.get("location", {}).get("lat", 44.6476), config.get("location", {}).get("lon", -63.5728), get_session())
        garbage = await get_tomorrow_collection(config["recollect_ics_url"], TZ, get_session()) if config.get("recollect_ics_url") else None
        embed = build_summary_embed(events, weather, garbage)
        await interaction.followup.send(embed=embed)


    @tree.command(name="today", description="Alias for /summary")
    async def cmd_today(interaction: discord.Interaction):
        await cmd_summary.callback(interaction)


    @tree.command(name="rsvps", description="See who confirmed for an event")
    @app_commands.describe(event_name="Part of the event name to search for")
    async def cmd_rsvps(interaction: discord.Interaction, event_name: str):
        await interaction.response.defer()
        events = await cal.get_events_for_days(14)
        name = event_name.lower()
        matches = [e for e in events if name in e["summary"].lower()]
        if not matches:
            await interaction.followup.send(f"❌ No upcoming events found matching `{event_name}`")
            return
        event = matches[0]
        rsvps = await get_database().get_rsvps(event["id"])
        if not rsvps:
            await interaction.followup.send(f"🤔 No RSVPs yet for **{event['summary']}**")
            return
        lines = []
        for r in rsvps:
            emoji = {"yes": "✅", "no": "❌", "maybe": "🤔"}.get(r["status"], "❓")
            lines.append(f"{emoji} {r['name']}")
        embed = discord.Embed(title=f"RSVPs: {event['summary']}", description="\n".join(lines), color=discord.Color.blue())
        await interaction.followup.send(embed=embed)


    @tree.command(name="week", description="Post this week's schedule right now")
    async def cmd_week(interaction: discord.Interaction):
        await interaction.response.defer()
        now = datetime.now(TZ)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        events = await cal.get_events_starting(tomorrow, 7)
        embed = build_weekly_embed(events, tomorrow)
        await interaction.followup.send(embed=embed)


    @tree.command(name="school_schedule", description="Show or hide Child1's school calendar in the daily summary")
    @app_commands.describe(setting="on = show classes in daily schedule; off = summer/hide from summary")
    @app_commands.choices(setting=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    async def cmd_school_schedule(interaction: discord.Interaction, setting: str):
        if get_person_group(interaction.user) not in ("admin", "parents"):
            await interaction.response.send_message("❌ Parents or admin only.", ephemeral=True)
            return
        enabled = setting == "on"
        await interaction.response.defer(ephemeral=True)
        try:
            await update_config({"show_school_in_daily_summary": enabled})
        except Exception as exc:
            log.error("school_schedule update_config failed: %s", exc, exc_info=True)
            await interaction.followup.send("❌ Failed to save setting. Check bot logs.", ephemeral=True)
            return
        if enabled:
            msg = "✅ School calendar **on** in daily summary and general schedule tools."
        else:
            msg = "✅ School calendar **off** for daily summary (summer mode). Use `/school` to check classes anytime."
        await interaction.followup.send(msg, ephemeral=True)


    @tree.command(name="addevent", description="Add an event to the family calendar (Bernie will help you)")
    async def cmd_addevent(interaction: discord.Interaction):
        channel_id = config.get("schedule_channel_id")
        msg = f"Just tell me what you want to add in <#{channel_id}>! For example: \"Add soccer practice tomorrow at 5pm\""
        await interaction.response.send_message(msg, ephemeral=True)


    @tree.command(name="setreminder", description="Set custom reminder times for an event")
    @app_commands.describe(
        event_name="Part of the event name to search for",
        minutes="Minutes before event, separated by commas (e.g. 60,15)"
    )
    async def cmd_setreminder(interaction: discord.Interaction, event_name: str, minutes: str):
        await interaction.response.defer()
        events = await cal.get_events_for_days(14)
        name = event_name.lower()
        matches = [e for e in events if name in e["summary"].lower()]
        if not matches:
            await interaction.followup.send(f"❌ No upcoming events found matching `{event_name}`")
            return
        event = matches[0]

        try:
            reminders = [int(m.strip()) for m in minutes.split(",")]
            tag = f"[remind:{','.join(map(str, sorted(reminders, reverse=True)))}]"
        except ValueError:
            await interaction.followup.send("❌ Invalid minutes format. Use numbers separated by commas.")
            return

        desc = event.get("description", "") or ""
        if "[remind:" in desc:
            desc = re.sub(r"\[remind:.*?\]", tag, desc)
        else:
            desc = (desc + "\n\n" + tag).strip()

        try:
            await cal.patch_event_description(event["calendar_id"], event["id"], desc)
            await interaction.followup.send(f"✅ Reminders for **{event['summary']}** set to: {', '.join(map(str, reminders))} minutes before.")
        except Exception as e:
            log.error(f"Error setting reminder: {e}")
            await interaction.followup.send("❌ Failed to update the event. Check logs.")

