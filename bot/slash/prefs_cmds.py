"""Slash commands: prefs (family-bot-1od — real module, no exec)."""
from __future__ import annotations

from typing import Any

import discord
from discord import app_commands


def register(tree: app_commands.CommandTree, m: Any) -> None:
    """Register prefs slash commands. *m* is the bot module (helpers + config)."""
    # Explicit bot bindings (replaces exec + _LiveNS proxy)
    _container = getattr(m, '_container', None)
    _discord_to_person_id = m._discord_to_person_id
    db_writes = m.db_writes
    get_database = m.get_database
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

    @tree.command(name="reminders", description="Toggle whether Bernie pings you in the channel for reminders")
    @app_commands.describe(setting="on or off")
    @app_commands.choices(setting=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    async def cmd_reminders(interaction: discord.Interaction, setting: str):
        enabled = setting == "on"
        await db_writes.set_person_pref(_discord_to_person_id(interaction.user.id) or str(interaction.user.id), discord_id=interaction.user.id, reminders_enabled=enabled)
        msg = "✅ You'll be pinged for reminders." if enabled else "🔕 You won't be pinged for reminders. The daily brief is unaffected."
        await interaction.response.send_message(msg, ephemeral=True)
        # If toggling on, flush any queued messages (catches quiet-hours queue)
        if enabled and _container:
            asyncio.create_task(_container.notification_orchestrator.flush_pending(str(interaction.user.id)))


    @tree.command(name="dm", description="Get personal reminders via DM instead of channel @mention")
    @app_commands.describe(setting="on or off")
    @app_commands.choices(setting=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    async def cmd_dm(interaction: discord.Interaction, setting: str):
        enabled = setting == "on"
        await db_writes.set_person_pref(_discord_to_person_id(interaction.user.id) or str(interaction.user.id), discord_id=interaction.user.id, dm_mode=enabled)
        msg = "📨 You'll receive personal reminders via DM." if enabled else "💬 Reminders will mention you in the channel."
        await interaction.response.send_message(msg, ephemeral=True)


    @tree.command(name="settings", description="View your personal Bernie preferences")
    async def cmd_settings(interaction: discord.Interaction):
        prefs = await get_database().get_person_pref(discord_id=interaction.user.id)
        embed = discord.Embed(title="⚙️ Your Bernie Settings", color=discord.Color.blurple())
        embed.add_field(
            name="🔔 Reminder pings",
            value=("✅ On" if prefs["reminders_enabled"] else "❌ Off") + " — `/reminders on` / `/reminders off`",
            inline=False
        )
        embed.add_field(
            name="📨 DM mode",
            value=("✅ On" if prefs["dm_mode"] else "❌ Off") + " — `/dm on` / `/dm off`",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

